"""Reproductions for checkout review candidates (batch 1).

Each test asserts the *buggy* behaviour, so "passed" == "bug reproduced".
"""

from decimal import Decimal
from unittest import mock

import graphene
import pytest
from django.db import IntegrityError
from django.db.models import F

from saleor.checkout.delivery_context import (
    assign_delivery_method_to_checkout,
    fetch_shipping_methods_for_checkout,
)
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import Checkout, CheckoutDelivery, CheckoutLine
from saleor.graphql.tests.utils import get_graphql_content
from saleor.shipping.models import ShippingMethod, ShippingMethodChannelListing


def _info(checkout, manager):
    lines, _ = fetch_checkout_lines(checkout)
    return fetch_checkout_info(checkout, lines, manager), lines


# --- R1: FILTER_SHIPPING_METHODS exclusion without a reason is ignored --------


@pytest.mark.parametrize("payload_extra", [{}, {"reason": ""}, {"reason": None}])
@mock.patch("saleor.webhook.transport.synchronous.transport.send_webhook_request_sync")
def test_r1_exclusion_without_reason_is_ignored(
    mocked_webhook,
    payload_extra,
    checkout_with_item,
    plugins_manager,
    address,
    app_exclude_shipping_for_checkout,
):
    shipping_method = ShippingMethod.objects.get()
    mocked_webhook.return_value = {
        "excluded_methods": [
            {
                "id": graphene.Node.to_global_id("ShippingMethod", shipping_method.id),
                **payload_extra,
            }
        ]
    }
    checkout = checkout_with_item
    checkout.shipping_address = address
    checkout.save(update_fields=["shipping_address"])
    checkout_info, _ = _info(checkout, plugins_manager)

    deliveries = fetch_shipping_methods_for_checkout(
        checkout_info, requestor=None
    ).get()

    mocked_webhook.assert_called_once()
    assert len(deliveries) == 1
    # BUG: the app excluded this method, yet it is still active/selectable.
    assert deliveries[0].built_in_shipping_method_id == shipping_method.id
    assert deliveries[0].active is True


@mock.patch("saleor.webhook.transport.synchronous.transport.send_webhook_request_sync")
def test_r1_control_exclusion_with_reason_works(
    mocked_webhook,
    checkout_with_item,
    plugins_manager,
    address,
    app_exclude_shipping_for_checkout,
):
    shipping_method = ShippingMethod.objects.get()
    mocked_webhook.return_value = {
        "excluded_methods": [
            {
                "id": graphene.Node.to_global_id("ShippingMethod", shipping_method.id),
                "reason": "nope",
            }
        ]
    }
    checkout = checkout_with_item
    checkout.shipping_address = address
    checkout.save(update_fields=["shipping_address"])
    checkout_info, _ = _info(checkout, plugins_manager)

    deliveries = fetch_shipping_methods_for_checkout(
        checkout_info, requestor=None
    ).get()

    assert len(deliveries) == 1
    assert deliveries[0].active is False


# --- R2: checkoutLineDelete deletes a line that belongs to another checkout ---

MUTATION_CHECKOUT_LINE_DELETE = """
    mutation checkoutLineDelete($id: ID, $lineId: ID!) {
        checkoutLineDelete(id: $id, lineId: $lineId) {
            checkout { token lines { id quantity } }
            errors { field message code }
        }
    }
"""


def test_r2_line_delete_removes_line_of_other_checkout(
    api_client, checkout_with_item, channel_USD
):
    # given: attacker checkout with the same variant + quantity as the victim line
    victim_checkout = checkout_with_item
    victim_line = victim_checkout.lines.get()

    attacker_checkout = Checkout.objects.create(
        channel=channel_USD, currency=channel_USD.currency_code
    )
    attacker_line = CheckoutLine.objects.create(
        checkout=attacker_checkout,
        variant=victim_line.variant,
        quantity=victim_line.quantity,
        currency=channel_USD.currency_code,
    )

    variables = {
        "id": graphene.Node.to_global_id("Checkout", attacker_checkout.pk),
        "lineId": graphene.Node.to_global_id("CheckoutLine", victim_line.pk),
    }

    # when
    response = api_client.post_graphql(MUTATION_CHECKOUT_LINE_DELETE, variables)

    # then
    content = get_graphql_content(response)
    data = content["data"]["checkoutLineDelete"]
    assert not data["errors"]
    # BUG: the victim's line is gone, the attacker's own line is untouched.
    assert not CheckoutLine.objects.filter(pk=victim_line.pk).exists()
    assert CheckoutLine.objects.filter(pk=attacker_line.pk).exists()
    assert victim_checkout.lines.count() == 0


def test_r2_control_line_of_other_checkout_with_different_quantity_is_kept(
    api_client, checkout_with_item, channel_USD
):
    victim_checkout = checkout_with_item
    victim_line = victim_checkout.lines.get()
    attacker_checkout = Checkout.objects.create(
        channel=channel_USD, currency=channel_USD.currency_code
    )
    CheckoutLine.objects.create(
        checkout=attacker_checkout,
        variant=victim_line.variant,
        quantity=victim_line.quantity + 1,
        currency=channel_USD.currency_code,
    )
    variables = {
        "id": graphene.Node.to_global_id("Checkout", attacker_checkout.pk),
        "lineId": graphene.Node.to_global_id("CheckoutLine", victim_line.pk),
    }
    response = api_client.post_graphql(MUTATION_CHECKOUT_LINE_DELETE, variables)
    get_graphql_content(response)
    assert CheckoutLine.objects.filter(pk=victim_line.pk).exists()


# --- R3: leftover invalid CheckoutDelivery rows -> IntegrityError -------------


def _bump_price(shipping_method, channel_id):
    ShippingMethodChannelListing.objects.filter(
        shipping_method=shipping_method, channel_id=channel_id
    ).update(price_amount=F("price_amount") + Decimal(1))


def _prepare_checkout_with_leftover_invalid_delivery(checkout, address, manager):
    shipping_method = ShippingMethod.objects.get()
    checkout.shipping_address = address
    checkout.save(update_fields=["shipping_address"])

    # 1. first fetch + customer picks the method
    checkout_info, lines = _info(checkout, manager)
    first = fetch_shipping_methods_for_checkout(checkout_info, requestor=None).get()[0]
    assign_delivery_method_to_checkout(checkout_info, lines, manager, first)

    # 2. price changes, deliveryOptionsCalculate (preserve mode) is called
    _bump_price(shipping_method, checkout.channel_id)
    checkout = Checkout.objects.get(pk=checkout.pk)
    checkout_info, lines = _info(checkout, manager)
    refreshed = fetch_shipping_methods_for_checkout(
        checkout_info, requestor=None, overwrite_assigned_delivery=False
    ).get()
    assert len(refreshed) == 1
    second = refreshed[0]
    assert second.pk != first.pk
    assert CheckoutDelivery.objects.get(pk=first.pk).is_valid is False

    # 3. customer picks the refreshed option
    assign_delivery_method_to_checkout(checkout_info, lines, manager, second)
    checkout = Checkout.objects.get(pk=checkout.pk)
    assert checkout.assigned_delivery_id == second.pk
    # the previously assigned, now invalid and unassigned row is still there
    assert CheckoutDelivery.objects.filter(pk=first.pk, is_valid=False).exists()
    return checkout, shipping_method


def test_r3_preserve_mode_crashes_on_second_price_change(
    checkout_with_item, plugins_manager, address
):
    checkout, shipping_method = _prepare_checkout_with_leftover_invalid_delivery(
        checkout_with_item, address, plugins_manager
    )
    _bump_price(shipping_method, checkout.channel_id)
    checkout_info, _ = _info(checkout, plugins_manager)

    with pytest.raises(IntegrityError, match="unique_for_checkout"):
        fetch_shipping_methods_for_checkout(
            checkout_info, requestor=None, overwrite_assigned_delivery=False
        ).get()


def test_r3_overwrite_mode_crashes_when_method_becomes_unavailable(
    checkout_with_item, plugins_manager, address
):
    checkout, shipping_method = _prepare_checkout_with_leftover_invalid_delivery(
        checkout_with_item, address, plugins_manager
    )
    ShippingMethodChannelListing.objects.filter(
        shipping_method=shipping_method
    ).delete()
    checkout_info, _ = _info(checkout, plugins_manager)

    with pytest.raises(IntegrityError, match="unique_for_checkout"):
        fetch_shipping_methods_for_checkout(checkout_info, requestor=None).get()


# --- R4: checkoutLinesUpdate changes indexed lines, index not marked dirty ----

MUTATION_CHECKOUT_LINES_UPDATE = """
    mutation checkoutLinesUpdate($id: ID, $lines: [CheckoutLineUpdateInput!]!) {
        checkoutLinesUpdate(id: $id, lines: $lines) {
            checkout { token lines { id quantity variant { id } } }
            errors { field code message }
        }
    }
"""


def test_r4_lines_update_removing_line_does_not_mark_search_dirty(
    api_client, checkout_with_item
):
    checkout = checkout_with_item
    line = checkout.lines.get()
    Checkout.objects.filter(pk=checkout.pk).update(search_index_dirty=False)

    variables = {
        "id": graphene.Node.to_global_id("Checkout", checkout.pk),
        "lines": [
            {
                "variantId": graphene.Node.to_global_id(
                    "ProductVariant", line.variant_id
                ),
                "quantity": 0,
            }
        ],
    }
    response = api_client.post_graphql(MUTATION_CHECKOUT_LINES_UPDATE, variables)
    data = get_graphql_content(response)["data"]["checkoutLinesUpdate"]
    assert not data["errors"]
    assert checkout.lines.count() == 0
    checkout.refresh_from_db()
    # BUG: indexed line content (sku / product / variant name) changed, flag not set
    assert checkout.search_index_dirty is False


def test_r4_lines_update_adding_new_variant_does_not_mark_search_dirty(
    api_client, checkout_with_item, product_with_two_variants
):
    checkout = checkout_with_item
    new_variant = product_with_two_variants.variants.first()
    Checkout.objects.filter(pk=checkout.pk).update(search_index_dirty=False)

    variables = {
        "id": graphene.Node.to_global_id("Checkout", checkout.pk),
        "lines": [
            {
                "variantId": graphene.Node.to_global_id(
                    "ProductVariant", new_variant.pk
                ),
                "quantity": 1,
            }
        ],
    }
    response = api_client.post_graphql(MUTATION_CHECKOUT_LINES_UPDATE, variables)
    data = get_graphql_content(response)["data"]["checkoutLinesUpdate"]
    assert not data["errors"]
    assert checkout.lines.count() == 2
    checkout.refresh_from_db()
    assert checkout.search_index_dirty is False
