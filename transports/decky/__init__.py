"""The Decky transport: everything about it that is not `decky` itself.

`plugin/main.py` is four lines of glue that Decky Loader imports by path; this
package is where the work is, so it can be tested on a machine that has no Steam
Deck and no `decky` module to import. `tests/test_transport_decky.py` exercises it
end to end without either.

Split the same way `transports/http` is: `dispatch.py` decides what a call means,
this decides how bytes arrive.
"""

from transports.decky.backend import DeckyBackend, install_decky_logging

__all__ = ["DeckyBackend", "install_decky_logging"]
