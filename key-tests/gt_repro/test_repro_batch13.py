"""Second-reviewer candidates. "passed" == bug reproduced."""

import datetime
from decimal import Decimal

import graphene
from django.db.models import F
from django.utils import timezone

from saleor.checkout import calculations
from saleor.checkout.delivery_context import (
    assign_delivery_method_to_checkout,
    fetch_shipping_methods_for_checkout,
    get_or_fetch_checkout_deliveries,
)
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import Checkout, CheckoutDelivery
from saleor.checkout.utils import add_voucher_to_checkout
from saleor.discount.models import CheckoutDiscount, VoucherCode
from saleor.graphql.tests.utils import get_graphql_content
from saleor.shipping.models import ShippingMethod, ShippingMethodChannelListing


def _info(checkout, manager):
    lines, _ = fetch_checkout_lines(checkout)
    return fetch_checkout_info(checkout, lines, manager), lines


def _gid(checkout):
    return graphene.Node.to_global_id("Checkout", checkout.pk)


# --- D1: removing a voucher by ID re-applies its discount ---------------------

REMOVE_PROMO = """
    mutation($id: ID, $promoCodeId: ID) {
        checkoutRemovePromoCode(id: $id, promoCodeId: $promoCodeId) {
            checkout { voucherCode discount { amount } discountName }
            errors { field code message }
        }
    }
"""


def test_d1_remove_voucher_by_id_leaves_discount(
    api_client, checkout_with_voucher, voucher
):
    checkout = checkout_with_voucher
    variables = {
        "id": _gid(checkout),
        "promoCodeId": graphene.Node.to_global_id("Voucher", voucher.id),
    }
    data = get_graphql_content(api_client.post_graphql(REMOVE_PROMO, variables))[
        "data"
    ]["checkoutRemovePromoCode"]
    assert not data["errors"]
    checkout.refresh_from_db()
    print("D1 response:", data["checkout"], "db discount:", checkout.discount_amount)
    assert checkout.voucher_code is None
    # BUG: voucher is gone, its discount amount and name are written back
    assert checkout.discount_amount > 0
    assert checkout.discount_name == voucher.name
    assert data["checkout"]["voucherCode"] is None
    assert data["checkout"]["discount"]["amount"] > 0


# --- D2: Checkout.voucher resolver crashes on a dangling voucher code ---------

QUERY_VOUCHER = """
    query($id: ID) { checkout(id: $id) { id voucher { id } } }
"""


def test_d2_voucher_resolver_crashes_for_deleted_code(
    staff_api_client, checkout_with_voucher, voucher, permission_manage_discounts
):
    checkout = checkout_with_voucher
    VoucherCode.objects.filter(voucher=voucher).delete()
    assert Checkout.objects.get(pk=checkout.pk).voucher_code is not None

    response = staff_api_client.post_graphql(
        QUERY_VOUCHER,
        {"id": _gid(checkout)},
        permissions=[permission_manage_discounts],
        check_no_permissions=False,
    )
    content = response.json()
    print("D2:", content.get("errors"))
    assert len(content["errors"]) == 1
    assert content["errors"][0]["extensions"]["exception"]["code"] == "AttributeError"
    assert content["errors"][0]["path"] == ["checkout", "voucher"]


# --- D3: min() of empty sequence with an apply-once-per-order voucher ---------

LINES_DELETE = """
    mutation checkoutLinesDelete($id: ID, $linesIds: [ID!]!) {
        checkoutLinesDelete(id: $id, linesIds: $linesIds) {
            checkout { id }
            errors { field code message }
        }
    }
"""


def test_d3_deleting_last_line_with_once_per_order_voucher_crashes(
    api_client, checkout_with_voucher, voucher
):
    voucher.apply_once_per_order = True
    voucher.save(update_fields=["apply_once_per_order"])
    checkout = checkout_with_voucher
    line = checkout.lines.get()
    Checkout.objects.filter(pk=checkout.pk).update(search_index_dirty=False)
    before = Checkout.objects.get(pk=checkout.pk)

    response = api_client.post_graphql(
        LINES_DELETE,
        {
            "id": _gid(checkout),
            "linesIds": [graphene.Node.to_global_id("CheckoutLine", line.pk)],
        },
    )
    content = response.json()
    print("D3:", content.get("errors"))
    # BUG: unhandled ValueError surfaces as a GraphQL error...
    assert len(content["errors"]) == 1
    assert content["errors"][0]["extensions"]["exception"]["code"] == "ValueError"
    # ...after the line was already deleted, with none of the follow-up bookkeeping
    after = Checkout.objects.get(pk=checkout.pk)
    assert after.lines.count() == 0
    assert after.price_expiration == before.price_expiration
    assert after.delivery_methods_stale_at == before.delivery_methods_stale_at
    assert after.search_index_dirty is False


# --- D4: checkoutCreate with validationRules: null ----------------------------

CHECKOUT_CREATE = """
    mutation createCheckout($checkoutInput: CheckoutCreateInput!) {
      checkoutCreate(input: $checkoutInput) {
        checkout { id }
        errors { field code message }
      }
    }
"""


def test_d4_checkout_create_with_null_validation_rules_crashes(
    api_client, stock, channel_USD
):
    variant_id = graphene.Node.to_global_id("ProductVariant", stock.product_variant_id)
    variables = {
        "checkoutInput": {
            "channel": channel_USD.slug,
            "lines": [{"quantity": 1, "variantId": variant_id}],
            "email": "customer@example.com",
            "validationRules": None,
        }
    }
    response = api_client.post_graphql(CHECKOUT_CREATE, variables)
    content = response.json()
    print("D4:", content.get("errors"))
    assert len(content["errors"]) == 1
    assert content["errors"][0]["extensions"]["exception"]["code"] == "AttributeError"
    assert Checkout.objects.count() == 0


# --- D5: free-shipping voucher is under-applied after a delivery price refresh --


def test_d5_shipping_voucher_stale_after_delivery_price_refresh(
    checkout_with_item, plugins_manager, address, voucher_free_shipping
):
    shipping_method = ShippingMethod.objects.get()
    checkout = checkout_with_item
    checkout.shipping_address = address
    checkout.save(update_fields=["shipping_address"])
    checkout_info, lines = _info(checkout, plugins_manager)
    delivery = fetch_shipping_methods_for_checkout(checkout_info, requestor=None).get()[0]
    assign_delivery_method_to_checkout(checkout_info, lines, plugins_manager, delivery)
    add_voucher_to_checkout(
        plugins_manager,
        checkout_info,
        lines,
        voucher_free_shipping,
        voucher_free_shipping.codes.first(),
    )
    old_price = delivery.price_amount
    assert Checkout.objects.get(pk=checkout.pk).discount_amount == old_price

    # shipping gets 5 more expensive; deliveries are refreshed (legacy/overwrite flow)
    ShippingMethodChannelListing.objects.filter(shipping_method=shipping_method).update(
        price_amount=F("price_amount") + Decimal(5)
    )
    Checkout.objects.filter(pk=checkout.pk).update(
        delivery_methods_stale_at=timezone.now() - datetime.timedelta(seconds=1)
    )
    checkout = Checkout.objects.get(pk=checkout.pk)
    checkout_info, lines = _info(checkout, plugins_manager)
    get_or_fetch_checkout_deliveries(checkout_info, requestor=None).get()

    checkout = Checkout.objects.get(pk=checkout.pk)
    assert CheckoutDelivery.objects.get(pk=delivery.pk).price_amount == old_price + 5
    # BUG: the voucher discount was "recalculated" against the old delivery price
    assert checkout.discount_amount == old_price

    # a later, completely fresh request charges 5 for "free shipping"
    checkout_info, lines = _info(checkout, plugins_manager)
    shipping_price = calculations.checkout_shipping_price(
        manager=plugins_manager,
        checkout_info=checkout_info,
        lines=lines,
    )
    print("D5 shipping price charged with 100% shipping voucher:", shipping_price.gross)
    assert shipping_price.gross.amount == Decimal(5)


# --- D6: lines-only query persists discount_expiration but not the discount ----

QUERY_LINES = "query($id: ID) { checkout(id: $id) { lines { id } } }"
QUERY_TOTAL = """
    query($id: ID) { checkout(id: $id) {
        discount { amount } totalPrice { gross { amount } } subtotalPrice { gross { amount } }
    } }
"""


def test_d6_lines_only_query_then_prices_ignore_new_order_promotion(
    api_client, checkout_with_item, order_promotion_rule
):
    checkout = checkout_with_item
    past = timezone.now() - datetime.timedelta(minutes=5)
    Checkout.objects.filter(pk=checkout.pk).update(
        price_expiration=past, discount_expiration=past
    )
    assert not CheckoutDiscount.objects.filter(checkout=checkout).exists()

    # 1. cart badge style query: only lines
    get_graphql_content(api_client.post_graphql(QUERY_LINES, {"id": _gid(checkout)}))
    after_lines = Checkout.objects.get(pk=checkout.pk)
    discount = CheckoutDiscount.objects.get(checkout=checkout)
    assert discount.amount_value > 0
    # discount object exists, expiration bumped, but checkout.discount_amount not saved
    assert after_lines.discount_expiration > timezone.now()
    assert after_lines.discount_amount == 0

    # 2. next request asks for prices
    data = get_graphql_content(
        api_client.post_graphql(QUERY_TOTAL, {"id": _gid(checkout)})
    )["data"]["checkout"]
    after_total = Checkout.objects.get(pk=checkout.pk)
    print(
        "D6:", data, "promotion discount object:", discount.amount_value,
        "price_expiration bumped:", after_total.price_expiration > timezone.now(),
    )
    # BUG: totals are persisted without the 25% order promotion
    assert data["discount"]["amount"] == 0
    assert data["totalPrice"]["gross"]["amount"] == data["subtotalPrice"]["gross"]["amount"]
    assert Decimal(str(data["subtotalPrice"]["gross"]["amount"])) == sum(
        l.undiscounted_unit_price_amount * l.quantity for l in checkout.lines.all()
    )
    assert after_total.price_expiration > timezone.now()


# --- D7: checkout(id:) with an integer-style global id ------------------------


def test_d7_checkout_query_with_integer_id_raises(api_client):
    response = api_client.post_graphql(
        "query($id: ID) { checkout(id: $id) { id } }",
        {"id": graphene.Node.to_global_id("Checkout", 123)},
    )
    content = response.json()
    print("D7:", content.get("errors"))
    assert len(content["errors"]) == 1
