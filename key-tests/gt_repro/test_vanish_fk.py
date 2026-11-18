"""Stale foreign keys written after the referenced row vanished (real commits)."""

from decimal import Decimal
from unittest import mock

import graphene
import pytest

from saleor.checkout.delivery_context import fetch_shipping_methods_for_checkout
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import Checkout, CheckoutDelivery
from saleor.graphql.payment.tests.mutations.test_transaction_initialize import (
    TRANSACTION_INITIALIZE,
)
from saleor.order.models import Order
from saleor.payment.models import TransactionItem
from saleor.plugins.manager import get_plugins_manager
from saleor.shipping.models import ShippingMethod, ShippingMethodChannelListing
from saleor.tests import race_condition
from .test_vanish_flows import (  # noqa: F401
    APP_IDENTIFIER,
    _complete_elsewhere,
    _gid,
    _raised,
    _unhandled,
    paid_checkout,
)


def _refresh(checkout_pk, overwrite):
    manager = get_plugins_manager(allow_replica=False)
    checkout = Checkout.objects.get(pk=checkout_pk)
    lines, _ = fetch_checkout_lines(checkout)
    info = fetch_checkout_info(checkout, lines, manager)
    return fetch_shipping_methods_for_checkout(
        info, requestor=None, overwrite_assigned_delivery=overwrite
    ).get()


# --- X1: delivery row dropped by a concurrent refresh, then assigned ----------

DELIVERY_UPDATE = """
mutation($id: ID, $m: ID){ checkoutDeliveryMethodUpdate(id:$id, deliveryMethodId:$m){
  checkout { id } errors { field code message } } }
"""


@pytest.mark.django_db(transaction=True)
def test_x1_delivery_removed_by_refresh_before_it_is_assigned(
    api_client, checkout_with_item, address, caplog
):
    checkout = checkout_with_item
    checkout.shipping_address = address
    checkout.save(update_fields=["shipping_address"])
    shipping_method = ShippingMethod.objects.get()
    delivery = _refresh(checkout.pk, overwrite=True)[0]  # options shown to customer

    def other_request_refreshes(*args, **kwargs):
        # method stops being offered (or an external app returns new rate ids) and
        # deliveryOptionsCalculate / a stale-triggered refresh runs in another request
        ShippingMethodChannelListing.objects.filter(
            shipping_method=shipping_method
        ).delete()
        _refresh(checkout.pk, overwrite=False)
        assert not CheckoutDelivery.objects.filter(pk=delivery.pk).exists()

    with race_condition.RunBefore(
        "saleor.graphql.checkout.mutations.checkout_delivery_method_update."
        "assign_delivery_method_to_checkout",
        other_request_refreshes,
    ):
        response = api_client.post_graphql(
            DELIVERY_UPDATE,
            {
                "id": _gid(checkout),
                "m": graphene.Node.to_global_id("CheckoutDelivery", delivery.pk),
            },
        )
    print("X1 client got:", _unhandled(response), "raised:", _raised(caplog))
    assert _raised(caplog) == ["IntegrityError"]
    assert Checkout.objects.get(pk=checkout.pk).assigned_delivery_id is None


# --- X2: transactionInitialize on a checkout completed a moment ago -----------


@pytest.mark.django_db(transaction=True)
@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_x2_transaction_initialize_reports_idempotency_conflict(
    mocked_initialize, api_client, paid_checkout, webhook_app, caplog
):
    checkout = paid_checkout
    webhook_app.identifier = APP_IDENTIFIER
    webhook_app.save()

    with race_condition.RunBefore(
        "saleor.graphql.payment.mutations.transaction.transaction_initialize."
        "cancel_active_payments",
        lambda *a, **k: _complete_elsewhere(checkout.pk),
    ):
        response = api_client.post_graphql(
            TRANSACTION_INITIALIZE,
            {
                "action": None,
                "amount": Decimal("10.00"),
                "id": _gid(checkout),
                "paymentGateway": {"id": APP_IDENTIFIER, "data": None},
                "idempotencyKey": "never-used-before",
            },
        )
    content = response.json()
    errors = (content.get("data") or {}).get("transactionInitialize", {}).get("errors")
    print("X2 client got:", _unhandled(response), errors, "raised:", _raised(caplog))
    mocked_initialize.assert_not_called()
    # BUG: the checkout is gone (completed); the client is told its brand-new
    # idempotency key collides with another transaction
    assert [(e["field"], e["code"]) for e in errors] == [("idempotencyKey", "UNIQUE")]
    assert not TransactionItem.objects.filter(
        idempotency_key="never-used-before"
    ).exists()


# --- X3: transactionCreate on a checkout completed a moment ago ----------------

TRANSACTION_CREATE = """
mutation($id: ID!, $transaction: TransactionCreateInput!){
  transactionCreate(id:$id, transaction:$transaction){
    transaction { id } errors { field code message } } }
"""


@pytest.mark.django_db(transaction=True)
def test_x3_transaction_create_after_checkout_completed(
    app_api_client, permission_manage_payments, paid_checkout, caplog
):
    checkout = paid_checkout
    app_api_client.app.permissions.add(permission_manage_payments)

    with race_condition.RunAfter(
        "saleor.graphql.payment.mutations.transaction.transaction_create."
        "TransactionCreate.validate_instance",
        lambda *a, **k: _complete_elsewhere(checkout.pk),
    ):
        response = app_api_client.post_graphql(
            TRANSACTION_CREATE,
            {
                "id": _gid(checkout),
                "transaction": {
                    "name": "Credit card",
                    "pspReference": "captured-at-psp",
                    "availableActions": [],
                    "amountCharged": {"amount": "10.00", "currency": "USD"},
                },
            },
        )
    content = response.json()
    print("X3 client got:", _unhandled(response),
          (content.get("data") or {}).get("transactionCreate"),
          "raised:", _raised(caplog))
    assert len(_unhandled(response)) == 1
    order = Order.objects.get()
    # the capture the app tried to record is on neither the checkout nor the order
    assert not TransactionItem.objects.filter(psp_reference="captured-at-psp").exists()
    assert order.payment_transactions.count() == 1
