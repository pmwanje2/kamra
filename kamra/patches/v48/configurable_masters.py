"""Taxonomies that were fixed Selects are Links to client masters (PMS
parity PR-5): Reservation market segment, source, booking type, booker
relation and cancellation reason; Folio Payment, Cashier Transaction and
Banquet Receipt mode; Meal Plan code.

The columns keep their values (a Select and a Link are both varchar), so no
record is rewritten. This seeds the masters with the values the Selects
offered, then adopts every other value already stored - an older release's
or a hand-edited one - as a hotel code, so every existing record still
saves and reports and filters group exactly as before.

Property, Property Setting, Turnover Profile and Cashier values are not
touched: a value saved on a live site stays what it is. Safe to run twice.
"""



def execute():
	# post_model_sync: the master DocTypes and the Link fields exist by now
	from kamra.masters import adopt_values_in_use, ensure_masters

	ensure_masters()
	adopt_values_in_use()
