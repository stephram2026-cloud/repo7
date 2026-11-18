"""A request/task loads a checkout, the checkout is completed by someone else while
it waits, and it then fails on the stale object.

The "someone else" is a REAL completion (complete_checkout, transaction flow) injected
at the point where the first actor is waiting. Single connection.
"""

import datetime
from decimal import Decimal
from unittest import mock

import graphene
import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from saleor.checkout import complete_checkout as cc
from saleor.checkout.calculations import fetch_checkout_data
from saleor.checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from saleor.checkout.models import Checkout, CheckoutDelivery, CheckoutLine
from saleor.core.db.connection import allow_writer
from saleor.checkout.tasks import automatic_checkout_completion_task
from saleor.graphql.checkout.tests.mutations.test_checkout_complete_with_transactions import (
    MUTATION_CHECKOUT_COMPLETE,
    prepare_checkout_for_test,
)
from saleor.graphql.payment.tests.mutations.test_transaction_initialize import (
    TRANSACTION_INITIALIZE,
)
from saleor.graphql.payment.tests.mutations.test_transaction_process import (
    TRANSACTION_PROCESS,
)
from saleor.order.models import Order
from saleor.payment import TransactionEventType
from saleor.payment.interface import TransactionSessionResult
from saleor.payment.models import TransactionEvent, TransactionItem
from saleor.payment.transaction_item_calculations import recalculate_transaction_amounts
from saleor.payment.utils import (
    create_transaction_event_from_request_and_webhook_response,
)
from saleor.plugins.manager import get_plugins_manager
from saleor.tests import race_condition
from saleor.warehouse.models import Stock

APP_IDENTIFIER = "webhook.app.identifier"


def _complete_elsewhere(checkout_pk):
    with allow_writer():
        return _complete_elsewhere_inner(checkout_pk)


def _complete_elsewhere_inner(checkout_pk):
    manager = get_plugins_manager(allow_replica=False)
    checkout = Checkout.objects.get(pk=checkout_pk)
    lines, _ = fetch_checkout_lines(checkout)
    info = fetch_checkout_info(checkout, lines, manager)
    order, _, _ = cc.complete_checkout(
        manager=manager,
        checkout_info=info,
        lines=lines,
        payment_data={},
        store_source=False,
        user=None,
        app=None,
    )
    assert order is not None
    return order


def _unhandled(response):
    # The failing statement in these tests is a Python-level "0 rows updated" check,
    # not failed SQL; clear Django's rollback flag so the test can inspect the DB.
    if connection.in_atomic_block:
        transaction.set_rollback(False)
    content = response.json()
    return [e["extensions"]["exception"]["code"] for e in content.get("errors") or []]


def _gid(checkout):
    return graphene.Node.to_global_id("Checkout", checkout.pk)


@pytest.fixture
def paid_checkout(
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
    Stock.objects.filter(product_variant=checkout.lines.get().variant).update(
        quantity=100
    )
    # normal state before "place order": prices fresh, checkout fully paid
    manager = get_plugins_manager(allow_replica=False)
    lines, _ = fetch_checkout_lines(checkout)
    info = fetch_checkout_info(checkout, lines, manager)
    fetch_checkout_data(info, manager, lines, None)
    checkout = Checkout.objects.get(pk=checkout.pk)
    assert checkout.authorize_status == "full"
    assert checkout.price_expiration > timezone.now()
    return checkout


def _expire_prices(checkout):
    past = timezone.now() - datetime.timedelta(minutes=1)
    Checkout.objects.filter(pk=checkout.pk).update(
        price_expiration=past, discount_expiration=past
    )


def _raised(caplog):
    """Qualified names of exceptions logged as unhandled by the GraphQL view."""
    return [
        r.exc_info[0].__qualname__
        for r in caplog.records
        if r.name == "saleor.graphql.errors.unhandled" and r.exc_info
    ]


# --- W1: checkoutLinesAdd: line is committed, then the final save crashes -----

LINES_ADD = """
mutation($id: ID, $v: ID!){ checkoutLinesAdd(id:$id, lines:[{variantId:$v, quantity:1}]){
  checkout { id } errors { field code message } } }
"""


@pytest.mark.django_db(transaction=True)
def test_w1_lines_add_error_although_line_went_into_the_order(
    api_client, paid_checkout, caplog
):
    checkout = paid_checkout
    line = checkout.lines.get()
    paid = checkout.payment_transactions.get().charged_value

    with race_condition.RunAfter(
        "saleor.graphql.checkout.mutations.checkout_lines_add.add_variants_to_checkout",
        lambda *a, **k: _complete_elsewhere(checkout.pk),
    ):
        response = api_client.post_graphql(
            LINES_ADD,
            {
                "id": _gid(checkout),
                "v": graphene.Node.to_global_id("ProductVariant", line.variant_id),
            },
        )

    got = _unhandled(response)
    order = Order.objects.get()
    order_line = order.lines.get()
    print(
        "W1 client got:", got,
        "| order line qty:", order_line.quantity, "(cart had", line.quantity, ")",
        "| order total:", order.total_gross_amount, "paid:", paid,
        "| order line total:", order_line.total_price_gross_amount,
    )
    print("W1 raised:", _raised(caplog))
    # client is told the add failed...
    assert _raised(caplog) == ["DatabaseError"]
    # ...but the added unit is in the order
    assert order_line.quantity == line.quantity + 1


# --- W2: transactionInitialize: charged, order updated, client gets a 500 -----


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_w2_transaction_initialize_500_after_successful_charge(
    mocked_initialize, api_client, paid_checkout, webhook_app,
    transaction_session_response, caplog,
):
    checkout = paid_checkout
    webhook_app.identifier = APP_IDENTIFIER
    webhook_app.save()
    amount = Decimal("10.00")
    response_data = {**transaction_session_response, "result": "CHARGE_SUCCESS",
                     "pspReference": "second-pay"}

    def app_call(*args, **kwargs):
        # while the payment app handles the request, the checkout (already fully
        # paid by the first transaction) is completed by another request / the
        # automatic completion task
        _complete_elsewhere(checkout.pk)
        return TransactionSessionResult(
            app_identifier=APP_IDENTIFIER, response=response_data
        )

    mocked_initialize.side_effect = app_call
    response = api_client.post_graphql(
        TRANSACTION_INITIALIZE,
        {
            "action": None,
            "amount": amount,
            "id": _gid(checkout),
            "paymentGateway": {"id": APP_IDENTIFIER, "data": None},
        },
    )

    order = Order.objects.get()
    new_transaction = TransactionItem.objects.get(psp_reference="second-pay")
    event_types = list(new_transaction.events.values_list("type", flat=True))
    print(
        "W2 client got:", _unhandled(response),
        "| new transaction charged:", new_transaction.charged_value,
        "order:", new_transaction.order_id == order.pk,
        "events:", event_types,
        "| order charge_status:", order.charge_status,
    )
    print("W2 raised:", _raised(caplog))
    assert _raised(caplog) == ["Checkout.DoesNotExist"]
    # the charge was recorded and attached to the order
    assert new_transaction.charged_value == amount
    assert new_transaction.order_id == order.pk
    assert TransactionEventType.CHARGE_SUCCESS in event_types


# --- W3: transactionProcess ---------------------------------------------------


@pytest.mark.parametrize(
    ("result", "expected_exception"),
    [("CHARGE_SUCCESS", "DoesNotExist"), ("CHARGE_ACTION_REQUIRED", None),
     ("CHARGE_FAILURE", None)],
)
@mock.patch("saleor.plugins.manager.PluginsManager.transaction_process_session")
def test_w3_transaction_process_after_checkout_completed(
    mocked_process, result, expected_exception, api_client, paid_checkout,
    webhook_app, transaction_session_response, transaction_item_generator, caplog,
):
    checkout = paid_checkout
    webhook_app.identifier = APP_IDENTIFIER
    webhook_app.save()
    amount = Decimal("10.00")
    transaction_item = transaction_item_generator(
        checkout_id=checkout.pk, app=webhook_app
    )
    TransactionEvent.objects.create(
        transaction=transaction_item,
        amount_value=amount,
        currency=transaction_item.currency,
        type=TransactionEventType.CHARGE_REQUEST,
    )
    response_data = {**transaction_session_response, "amount": amount,
                     "result": result, "pspReference": "3ds-return"}

    def app_call(*args, **kwargs):
        _complete_elsewhere(checkout.pk)
        return TransactionSessionResult(
            app_identifier=APP_IDENTIFIER, response=response_data
        )

    mocked_process.side_effect = app_call
    response = api_client.post_graphql(
        TRANSACTION_PROCESS,
        {
            "id": graphene.Node.to_global_id("TransactionItem", transaction_item.token),
            "data": None,
        },
    )
    transaction_item.refresh_from_db()
    events = list(transaction_item.events.values_list("type", flat=True))
    print(f"W3[{result}] client got:", _unhandled(response), "| events:", events,
          "| psp_reference:", transaction_item.psp_reference)
    print(f"W3[{result}] raised:", _raised(caplog))
    assert _raised(caplog) == ["Checkout.DoesNotExist"]
    # the app's answer was persisted before the crash
    assert transaction_item.psp_reference == "3ds-return"
    assert result.lower() in events


# --- W4/W5: storefront queries while the checkout completes --------------------


@pytest.mark.django_db(transaction=True)
def test_w4_total_price_query_while_checkout_completes(
    api_client, paid_checkout, caplog
):
    checkout = paid_checkout
    _expire_prices(checkout)  # e.g. a mutation just invalidated them
    with race_condition.RunAfter(
        "saleor.checkout.calculations.recalculate_discounts",
        lambda *a, **k: _complete_elsewhere(checkout.pk),
    ):
        response = api_client.post_graphql(
            "query($id: ID){ checkout(id:$id){ id totalPrice { gross { amount } } } }",
            {"id": _gid(checkout)},
        )
    print("W4 client got:", _unhandled(response), "raised:", _raised(caplog))
    assert _raised(caplog) == ["DatabaseError"]


def test_w5_lines_query_while_checkout_completes(api_client, paid_checkout, caplog):
    checkout = paid_checkout
    _expire_prices(checkout)
    with race_condition.RunBefore(
        "saleor.checkout.calculations.recalculate_discounts",
        lambda *a, **k: _complete_elsewhere(checkout.pk),
    ):
        response = api_client.post_graphql(
            "query($id: ID){ checkout(id:$id){ id lines { id } } }",
            {"id": _gid(checkout)},
        )
    print("W5 client got:", _unhandled(response), response.json().get("data"),
          "raised:", _raised(caplog))
    assert len(_unhandled(response)) == 1
    assert response.json()["data"] == {"checkout": None}


# --- W6/W7: second completer (double submit / automatic completion task) -------


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("expired", [False, True])
def test_w6_double_submit_of_checkout_complete_control(
    expired, api_client, paid_checkout, caplog
):
    checkout = paid_checkout
    if expired:
        _expire_prices(checkout)
    with race_condition.RunAfter(
        "saleor.checkout.calculations.recalculate_discounts",
        lambda *a, **k: _complete_elsewhere(checkout.pk),
    ):
        response = api_client.post_graphql(
            MUTATION_CHECKOUT_COMPLETE,
            {"id": _gid(checkout), "redirectUrl": "https://www.example.com"},
        )
    content = response.json()
    print("W6 client got:", _unhandled(response),
          (content.get("data") or {}).get("checkoutComplete"))
    got = _unhandled(response)
    print(f"W6[expired={expired}] raised:", _raised(caplog))
    assert Order.objects.count() == 1
    if expired:
        # BUG: prices had to be recalculated -> second submit gets a 500
        assert _raised(caplog) == ["DatabaseError"]
    else:
        # handled: second submit returns the existing order
        assert got == []
        assert content["data"]["checkoutComplete"]["order"]["token"] == str(
            Order.objects.get().pk
        )


def test_w7_automatic_completion_task_loses_to_the_customer(paid_checkout):
    checkout = paid_checkout
    _expire_prices(checkout)
    with race_condition.RunAfter(
        "saleor.checkout.calculations.recalculate_discounts",
        lambda *a, **k: _complete_elsewhere(checkout.pk),
    ):
        with pytest.raises(DatabaseError, match="did not affect any rows"):
            automatic_checkout_completion_task(checkout.pk)
    transaction.set_rollback(False)
    assert Order.objects.count() == 1


def test_w7b_task_starts_after_customer_completed(paid_checkout):
    """Task picked the checkout up, the customer completed it before the task's
    first query after its own (released) row lock."""
    checkout = paid_checkout
    with race_condition.RunBefore(
        "saleor.checkout.tasks.fetch_checkout_lines",
        lambda *a, **k: _complete_elsewhere(checkout.pk),
    ):
        try:
            automatic_checkout_completion_task(checkout.pk)
            outcome = "returned normally"
        except Exception as e:  # noqa: BLE001
            outcome = f"raised {type(e).__qualname__}: {str(e)[:80]}"
    print("W7b:", outcome)
    assert Order.objects.count() == 1


# --- W8: async action-webhook response (charge requested on a checkout) --------


def test_w8_action_response_task_after_checkout_completed(paid_checkout, webhook_app):
    checkout = paid_checkout
    transaction_item = checkout.payment_transactions.get()
    total = transaction_item.charged_value
    # the checkout is fully AUTHORIZED; staff/app requests the charge
    transaction_item.events.update(type=TransactionEventType.AUTHORIZATION_SUCCESS)
    recalculate_transaction_amounts(transaction_item)
    assert transaction_item.authorized_value == total
    request_event = TransactionEvent.objects.create(
        transaction=transaction_item,
        amount_value=total,
        currency=transaction_item.currency,
        type=TransactionEventType.CHARGE_REQUEST,
        include_in_calculations=False,
    )
    response = {"pspReference": "charge-1", "result": "CHARGE_SUCCESS",
                "amount": str(total)}

    with race_condition.RunBefore(
        "saleor.payment.utils.transaction_amounts_for_checkout_updated",
        lambda *a, **k: _complete_elsewhere(checkout.pk),
    ):
        try:
            create_transaction_event_from_request_and_webhook_response(
                request_event, webhook_app, response
            )
            outcome = "returned normally"
        except Exception as e:  # noqa: BLE001
            outcome = f"raised {type(e).__qualname__}: {str(e)[:80]}"
    transaction.set_rollback(False)
    order = Order.objects.get()
    transaction_item.refresh_from_db()
    print(
        "W8 task:", outcome,
        "| transaction charged:", transaction_item.charged_value,
        "| order total_charged:", order.total_charged_amount,
        "charge_status:", order.charge_status,
    )
    assert outcome.startswith("raised")
