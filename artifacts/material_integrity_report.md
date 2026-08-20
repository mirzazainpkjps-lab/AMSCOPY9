# Material Master Integrity Report

Read-only audit. No materials were merged, renamed, or deleted.

## Snapshot counts

| Table | Count |
| --- | ---: |
| `material` | 66 |
| `direct_sale` | 2410 |
| `direct_sale_item` | 4431 |
| `booking` | 387 |
| `booking_item` | 885 |
| `delivery` | 0 |
| `delivery_item` | 0 |
| `material_return` | 74 |
| `material_return_item` | 101 |
| `entry` | 4576 |
| `grn` | 48 |
| `grn_item` | 48 |

## Duplicate-like materials

Total materials: **66**
Normalized-name groups with more than one record: **1**

| Group | ID | Code | Name | Active | Stock | Unit | Price | GRN | Sales | Bookings | Returns | Entries |
| --- | ---: | --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `rentsteel` | 11 | FBMREN-000003 | RENT-STEEL | yes | -47359.24999999999 | Pcs | 0.0 | 0 | 79 | 50 | 0 | 77 |
| `rentsteel` | 43 | tmpm-00004 | RENT STEEL | yes | -15597.2 | Bags | 1.785714285719 | 0 | 33 | 11 | 0 | 33 |

## Policy

- Existing historical references are left untouched.
- Duplicate materials are not merged or deleted by this fix.
- New Sales / Dispatch / Booking / Return posts must select an existing Material Master record.

