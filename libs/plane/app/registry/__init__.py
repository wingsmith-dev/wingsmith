"""Registry / intent — CRUD over declared objects.

One package per declared object group, each following the same file layout:

    <name>/routes.py       Controller class, `PascalCase + Routes`
    <name>/schemas.py      msgspec request/response structs
    <name>/services.py     business logic, `PascalCase + Service`, owns the commit
    <name>/repository.py   database access, `PascalCase + Repository`, flushes only
    <name>/depends.py      service generator wired into the controller

Nothing lives here yet: the object model is authored elsewhere. This package is
the shape it lands in, and it is the only one of the three plane concerns that
is allowed to write declared state.
"""
