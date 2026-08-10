"""`appraisal` — the feature module that turns prices into verdicts.

`prices` answers *"what is this worth"*. This module answers the question the tool
exists for (SPEC §1): *"is any of this worth a stash trip, or is it all vendor
trash?"* — which is a different question, carries different policy, and belongs in a
different module so that `crafting` can have the number without inheriting the
opinion (IMPLEMENTATION-PLAN §1.3).
"""
