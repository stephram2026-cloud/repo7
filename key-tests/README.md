# Tests for the 18 graded checkout bugs

Repository: saleor/saleor at `b8025bac2a0bdcf9f23e65be1db72e6b7ecfe043`.

A passing test means the faulty behaviour was reproduced. All tests below pass at that commit (22 passed).

## How to run

1. Copy the `gt_repro` folder to `saleor/checkout/tests/gt_repro` in a checkout of the commit above.
2. Start Postgres and set `DATABASE_URL` and `SECRET_KEY`.
3. Run a test by its id, for example:

```
python -m pytest -n 0 --create-db -q -p no:cacheprovider \
  saleor/checkout/tests/gt_repro/test_repro_batch13.py::test_d3_deleting_last_line_with_once_per_order_voucher_crashes
```

Run only the ids listed here. The files also hold older tests that are not part of the graded set.

## Success criterion to test

| # | Bug | Test |
|---|---|---|
| 1 | Failed shipping app call stored for 24 hours | `test_repro_batch3.py::test_r10_failed_shipping_app_call_cached_for_full_ttl` |
| 2 | Running refresh overwrites a newer stale value | `test_repro_batch2.py::test_r5_inflight_refresh_overwrites_stale_marker` |
| 3 | Exclusion without reason is ignored | `test_repro_batch1.py::test_r1_exclusion_without_reason_is_ignored` |
| 4 | Subscription selecting shippingMethod / deliveryMethod loops | `test_repro_batch4.py::test_r11_subscription_with_assigned_delivery_field_recurses` |
| 5 | Deleted shipping method crashes completion | `test_repro_batch12.py::test_c8_deleted_shipping_method_crashes_completion` |
| 6 | Method excluded during completion is still used | `test_repro_batch9.py::test_c2_method_excluded_during_completion_refresh_is_still_used` |
| 7 | Underpaid order after cart change during completion | `test_repro_batch9.py::test_c1_order_created_underpaid_after_concurrent_lines_change` |
| 8 | Gift card deadlock between two checkouts | `test_judge_k13.py::test_judge_k13_two_checkouts_sharing_gift_card_complete_concurrently` |
| 9 | Quantity limit bypassed by concurrent adds | `test_repro_concurrent_lines.py::test_e4_quantity_limit_bypassed_by_concurrent_adds_bug` |
| 10 | checkoutLinesUpdate checks the wrong lines | `test_repro_batch5.py::test_r12_availability_validation_depends_on_input_order` |
| 11 | Last line with once per order voucher crashes | `test_repro_batch13.py::test_d3_deleting_last_line_with_once_per_order_voucher_crashes` |
| 12 | checkoutCreate with validationRules null crashes | `test_repro_batch13.py::test_d4_checkout_create_with_null_validation_rules_crashes` |
| 13 | checkout.voucher crashes for a deleted code | `test_repro_batch13.py::test_d2_voucher_resolver_crashes_for_deleted_code` |
| 14 | Discount remains after removing a code by ID | `test_repro_batch13.py::test_d1_remove_voucher_by_id_leaves_discount` |
| 15 | Lines only query makes the next total ignore a promotion | `test_repro_batch13.py::test_d6_lines_only_query_then_prices_ignore_new_order_promotion` |
| 16 | Price query fails when the checkout completes under it | `test_vanish_flows.py::test_w4_total_price_query_while_checkout_completes` |
| 17 | transactionUpdate fails when the checkout completes | `test_vanish_transactions.py::test_y2_transaction_update_while_checkout_completes` |
| 18 | transactionCreate fails on a just completed checkout | `test_vanish_fk.py::test_x3_transaction_create_after_checkout_completed` |
