"""Domain modules.

One package per object in the model. Everything that is not a domain object —
the database, configuration, logging, the HTTP application, the hub — lives
outside this package, because those are machinery and these are the product.

Each module follows the same file layout:

    <name>/routes.py       Controller class, `PascalCase + Routes`
    <name>/schemas.py      msgspec request/response structs
    <name>/services.py     business logic, `PascalCase + Service`, owns the commit
    <name>/repository.py   database access, `PascalCase + Repository`, flushes only
    <name>/depends.py      service generator wired into the controller

The declared/observed split from `contract/API.md` reaches into this layout: a
module may write declared state, and only ingestion writes observed state. A
module whose object is observed (nodes, devices, topology domains) is therefore
read-only, and its service exposes no create or update at all.
"""
