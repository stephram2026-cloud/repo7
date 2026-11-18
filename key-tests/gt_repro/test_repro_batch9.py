"""Completion-flow candidates (transaction flow). "passed" == bug reproduced."""

import datetime
from decimal import Decimal
from unittest import mock

import graphene
from django.db import transaction
from django.utils import timezone

from saleor.checkout import complete_checkout as cc
from saleor.checkout.error_codes import CheckoutErrorCode
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import Checkout, CheckoutDelivery, CheckoutLine
from saleor.giftcard.models import GiftCard
from saleor.graphql.checkout.tests.mutations.test_checkout_complete_with_transactions import (
    MUTATION_CHECKOUT_COMPLETE,
    prepare_checkout_for_test,
)
from saleor.graphql.core.utils import to_global_id_or_none
from saleor.graphql.tests.utils import get_graphql_content
from saleor.order import OrderAuthorizeStatus, OrderChargeStatus
from saleor.order.models import Order
from saleor.plugins.manager import get_plugins_manager
from saleor.tests import race_condition
from saleor.warehouse.models import Stock


def _complete(client, checkout):
    variables = {
        "id": to_global_id_or_none(checkout),
        "redirectUrl": "https://www.example.com",
    }
    response = client.post_graphql(MUTATION_CHECKOUT_COMPLETE, variables)
    return get_graphql_content(response)["data"]["checkoutComplete"]


# --- C1: nothing is re-validated after the lock in the transaction flow -------


def test_c1_order_created_underpaid_after_concurrent_lines_change(
    user_api_client,
    checkout_with_item,
    address,
    checkout_delivery,
    transaction_item_generator,
    transaction_events_generator,
):
    checkout = prepare_checkout_for_test(
        checkout_with_item,
        address,
        address,
        checkout_delivery(checkout_with_item),
        transaction_item_generator,
        transaction_events_generator,
    )
    assert checkout.channel.allow_unpaid_orders is False
    transaction_item = checkout.payment_transactions.get()
    paid = transaction_item.charged_value
    line = checkout.lines.get()
    Stock.objects.filter(product_variant=line.variant).update(quantity=100)

    def concurrent_lines_update(*args, **kwargs):
        # another request (checkoutLinesUpdate) commits after checkoutComplete passed
        # its "fully authorized" check but before it takes the checkout row lock
        CheckoutLine.objects.filter(pk=line.pk).update(quantity=line.quantity * 4)
        now = timezone.now()
        Checkout.objects.filter(pk=checkout.pk).update(
            price_expiration=now, discount_expiration=now
        )

    with race_condition.RunBefore(
        "saleor.checkout.complete_checkout.create_order_from_checkout",
        concurrent_lines_update,
    ):
        data = _complete(user_api_client, checkout)

    # BUG: the order is created for the bigger cart although only the old total is paid
    assert not data["errors"]
    order = Order.objects.get()
    assert order.lines.get().quantity == line.quantity * 4
    assert order.total.gross.amount > paid
    assert order.charge_status == OrderChargeStatus.PARTIAL
    assert not Checkout.objects.filter(pk=checkout.pk).exists()
    print(f"C1 paid={paid} order_total={order.total.gross.amount}")


# --- C2: refresh at completion excludes the method, order is created anyway ---


@mock.patch("saleor.webhook.transport.synchronous.transport.send_webhook_request_sync")
def test_c2_method_excluded_during_completion_refresh_is_still_used(
    mocked_webhook,
    user_api_client,
    checkout_with_item,
    address,
    checkout_delivery,
    transaction_item_generator,
    transaction_events_generator,
    app_exclude_shipping_for_checkout,
):
    delivery = checkout_delivery(checkout_with_item)
    checkout = prepare_checkout_for_test(
        checkout_with_item,
        address,
        address,
        delivery,
        transaction_item_generator,
        transaction_events_generator,
    )
    # deliveries are stale -> checkoutComplete refreshes them first
    Checkout.objects.filter(pk=checkout.pk).update(
        delivery_methods_stale_at=timezone.now() - datetime.timedelta(minutes=1)
    )
    reason = "Not available for this cart"
    mocked_webhook.return_value = {
        "excluded_methods": [
            {
                "id": graphene.Node.to_global_id(
                    "ShippingMethod", delivery.built_in_shipping_method_id
                ),
                "reason": reason,
            }
        ]
    }
    seen = {}

    def capture(*args, **kwargs):
        d = CheckoutDelivery.objects.get(pk=delivery.pk)
        seen["active"], seen["message"] = d.active, d.message

    with race_condition.RunBefore(
        "saleor.checkout.complete_checkout.create_order_from_checkout", capture
    ):
        data = _complete(user_api_client, checkout)

    mocked_webhook.assert_called_once()
    # the refresh did store the exclusion on the assigned delivery...
    assert seen == {"active": False, "message": reason}
    # BUG: ...but completion went through with the excluded method
    assert not data["errors"]
    order = Order.objects.get()
    assert order.shipping_method_id == delivery.built_in_shipping_method_id


# --- C4: voucher usage is consumed by a failed transaction-flow completion ----


def test_c4_single_use_voucher_burned_by_insufficient_stock(
    user_api_client,
    checkout_with_voucher_percentage,
    voucher_percentage,
    address,
    shipping_method,
    checkout_delivery,
    transaction_events_generator,
    transaction_item_generator,
):
    code = voucher_percentage.codes.first()
    voucher_percentage.single_use = True
    voucher_percentage.save(update_fields=["single_use"])
    checkout = prepare_checkout_for_test(
        checkout_with_voucher_percentage,
        address,
        address,
        checkout_delivery(checkout_with_voucher_percentage, shipping_method),
        transaction_item_generator,
        transaction_events_generator,
    )
    line = checkout.lines.get()
    stock = Stock.objects.get(product_variant=line.variant)
    original_quantity = stock.quantity
    stock.quantity = 0
    stock.save(update_fields=["quantity"])

    # first attempt: out of stock
    data = _complete(user_api_client, checkout)
    assert len(data["errors"]) == 1
    assert data["errors"][0]["code"] == CheckoutErrorCode.INSUFFICIENT_STOCK.name
    assert Order.objects.count() == 0

    # BUG: usage stays consumed although no order exists
    code.refresh_from_db()
    checkout.refresh_from_db()
    assert code.is_active is False
    assert checkout.is_voucher_usage_increased is True

    # stock is back, customer retries: the voucher they legitimately hold is rejected
    stock.quantity = original_quantity
    stock.save(update_fields=["quantity"])
    data = _complete(user_api_client, checkout)
    assert len(data["errors"]) == 1
    assert data["errors"][0]["code"] == CheckoutErrorCode.VOUCHER_NOT_APPLICABLE.name
    assert Order.objects.count() == 0


# --- C6: stale is_voucher_usage_increased -> usage counted twice --------------


def test_c6_double_submit_counts_voucher_usage_twice(
    checkout_with_voucher_percentage,
    voucher_percentage,
    address,
    shipping_method,
    checkout_delivery,
    transaction_events_generator,
    transaction_item_generator,
):
    voucher_percentage.usage_limit = 10
    voucher_percentage.save(update_fields=["usage_limit"])
    code = voucher_percentage.codes.first()
    checkout = prepare_checkout_for_test(
        checkout_with_voucher_percentage,
        address,
        address,
        checkout_delivery(checkout_with_voucher_percentage, shipping_method),
        transaction_item_generator,
        transaction_events_generator,
    )
    manager = get_plugins_manager(allow_replica=False)

    def load_request_state():
        c = Checkout.objects.get(pk=checkout.pk)
        lines, _ = fetch_checkout_lines(c)
        return fetch_checkout_info(c, lines, manager)

    # two checkoutComplete requests for the same checkout load their state
    info_r1 = load_request_state()
    info_r2 = load_request_state()

    def first_transaction_of_create_order_from_checkout(checkout_info):
        # verbatim body of the first atomic block in create_order_from_checkout
        with transaction.atomic():
            locked = (
                Checkout.objects.select_for_update()
                .filter(pk=checkout_info.checkout.pk)
                .first()
            )
            assert locked
            return cc._increase_voucher_code_usage_value(checkout_info=checkout_info)

    first_transaction_of_create_order_from_checkout(info_r1)
    # R2 was queued on the row lock and now gets it
    first_transaction_of_create_order_from_checkout(info_r2)

    code.refresh_from_db()
    # BUG: one checkout, usage counted twice
    assert code.used == 2
