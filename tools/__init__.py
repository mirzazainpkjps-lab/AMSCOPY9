"""
AMS System Tools Package
========================
Organized operational scripts for the AMS/FBM system.

Subpackages
-----------
read_only/          Safe read-only audit and inspection scripts.
repair_controlled/  Controlled repair scripts (ORM path, require --confirm).
deprecated/         Completed one-time migrations. Never run again.
tests_isolated/     Integration tests. Require AMS_TEST_DB env var.

Entry points
------------
tools/consistency_report.py   One-command system health check.
tools/db_write_guard.py       Write-path safety observer.
"""
