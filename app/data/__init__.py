"""
data — SQLite storage package for the DM engine / GUI side of the app.

Note the deliberate naming split:

  * ``data.db``        -> strategy + queue + tracker DB (this module).
                          Exposes ``get_connection()`` (singular) and ``init_db()``.
  * ``outreach.db``    -> the contact-form blast engine's own DB
                          (``outreach/blaster.db``), which exposes ``get_conn()``.

They are two different databases with two different functions. Importing the
wrong one gets you a *working* connection to the wrong file, which is why the
names are kept deliberately distinct.
"""

from data.db import get_connection, init_db

__all__ = ["get_connection", "init_db"]
