# Legacy migration schema-to-template mapping

The migration subsystem is intentionally additive and does not accept a legacy database dump. The current application model is the source of truth.

| Template | Current entity / source facts | Dependency | Derived fields deliberately excluded |
|---|---|---|---|
| Clients | `Client`: name, phone, address, category | None | client due/current balance |
| Suppliers | `Supplier`: name, phone, address | None | supplier due |
| Materials | `Material`: name, category, unit, unit price | None | stock total |
| Accounts | `Account`: name, category, type, opening baseline | None | calculated closing balance |
| GRN | `GRN` + `GRNItem`: supplier/date/account, material/qty/rate | Suppliers, Materials | stock movement/total |
| Bookings | `Booking` + `BookingItem`: client/date, material/qty/rate | Clients, Materials | reservation, due |
| Sales / Direct Sales | `DirectSale` + `DirectSaleItem`: party/date/account, material/qty/rate | Clients, Materials | FIFO stock allocation, ledger impact |
| Payments | `Payment` / `SupplierPayment`: party, account, amount, type | Accounts, party | ledgers/account balance |

Transaction templates are already worker-facing and validation-ready. Their import buttons remain explicitly locked until each is wired to its corresponding domain service; this prevents bypassing inventory allocation and ledger/account services with raw inserts.
