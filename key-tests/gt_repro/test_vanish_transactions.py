"""transactionEventReport / transactionUpdate while the checkout completes."""

from decimal import Decimal

import graphene
import pytest

from saleor.order.models import Order
from saleor.payment.models import TransactionItem
from saleor.tests import race_condition
from .test_vanish_flows import (  # noqa: F401
    _complete_elsewhere,
    _raised,
    _unhandled,
    paid_checkout,
)

T = "saleor.graphql.payment.mutations.transaction."

EVENT_REPORT = """
mutation($id: ID, $amount: PositiveDecimal!, $psp: String!){
  transactionEventReport(id:$id, type: CHARGE_SUCCESS, amount:$amount,
                         pspReference:$psp, availableActions: []){
    alreadyProcessed transaction { id } errors { field code message } } }
"""
TRANSACTION_UPDATE = """
mutation($id: ID, $transaction: TransactionUpdateInput){
  transactionUpdate(id:$id, transaction:$transaction){
    transaction { id } errors { field code message } } }
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "hook",
    [
        ("after", T + "transaction_event_report.get_transaction_item"),
        ("before", T + "transaction_event_report.process_order_or_checkout_with_transaction"),
    ],
    ids=["right-after-load", "before-amount-processing"],
)
def test_y1_event_report_while_checkout_completes_control(
    hook, app_api_client, permission_manage_payments, paid_checkout,
    transaction_item_generator, caplog,
):
    checkout = paid_checkout
    app_api_client.app.permissions.add(permission_manage_payments)
    item = transaction_item_generator(checkout_id=checkout.pk, app=app_api_client.app)
    when, target = hook
    trigger = race_condition.RunAfter if when == "after" else race_condition.RunBefore

    with trigger(target, lambda *a, **k: _complete_elsewhere(checkout.pk)):
        response = app_api_client.post_graphql(
            EVENT_REPORT,
            {
                "id": graphene.Node.to_global_id("TransactionItem", item.token),
                "amount": Decimal("5.00"),
                "psp": "late-capture",
            },
        )
    content = response.json()
    item = TransactionItem.objects.get(pk=item.pk)
    order = Order.objects.get()
    print(
        f"Y1[{when}] unhandled:", _unhandled(response), "raised:", _raised(caplog),
        "errors:", (content.get("data") or {}).get("transactionEventReport", {}).get("errors"),
        "| charged:", item.charged_value, "on order:", item.order_id == order.pk,
        "| order total_charged:", order.total_charged_amount,
    )
    # handled correctly: event recorded and applied to the order
    assert _unhandled(response) == []
    assert content["data"]["transactionEventReport"]["errors"] == []
    assert item.order_id == order.pk
    assert item.charged_value == Decimal("5.00")
    assert order.total_charged_amount == Decimal("5.00") + (
        order.total_gross_amount
    )


@pytest.mark.django_db(transaction=True)
def test_y2_transaction_update_while_checkout_completes(
    app_api_client, permission_manage_payments, paid_checkout,
    transaction_item_generator, caplog,
):
    checkout = paid_checkout
    app_api_client.app.permissions.add(permission_manage_payments)
    item = transaction_item_generator(checkout_id=checkout.pk, app=app_api_client.app)

    with race_condition.RunBefore(
        T + "transaction_update.TransactionUpdate.update_transaction",
        lambda *a, **k: _complete_elsewhere(checkout.pk),
    ):
        response = app_api_client.post_graphql(
            TRANSACTION_UPDATE,
            {
                "id": graphene.Node.to_global_id("TransactionItem", item.token),
                "transaction": {
                    "amountCharged": {"amount": "5.00", "currency": "USD"},
                },
            },
        )
    item = TransactionItem.objects.get(pk=item.pk)
    order = Order.objects.get()
    print(
        "Y2 unhandled:", _unhandled(response), "raised:", _raised(caplog),
        "| charged:", item.charged_value, "on order:", item.order_id == order.pk,
        "| order total_charged:", order.total_charged_amount,
    )
    # BUG: full-row save of the stale transaction writes the deleted checkout id
    assert _raised(caplog) == ["IntegrityError"]
    # the capture the app reported is lost
    assert item.charged_value == Decimal(0)
