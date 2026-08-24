"""Application-database layer for DB Buddy.

This package owns ALL platform state — users, roles, permissions, saved charts,
query history, ERP connection configs, audit logs, published reports — in a
dedicated application database that is completely separate from the customer
ERP databases. Customer databases are query targets only; nothing platform-
related is ever written to them.
"""
