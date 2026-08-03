"""Walkthrough of the feedback loop. Run it, or paste the blocks into a REPL.

    python simple/demo.py

Each block is one iteration: generate, look at the output, give feedback,
generate again. Feedback is never re-entered — it persists and keeps applying
in every later version.
"""
from feedback_loop import add_feedback, generate, report, reset, show

# Start clean so the demo is repeatable.
reset()

# --- iteration 1 -----------------------------------------------------------
generate()          # v1
show()

# Feedback on two columns in `customers`, one in `orders`.
# Default scope is the column it was given on.
add_feedback("customers.cust_id", "too verbose, and it just restates the type. Keep it under 10 words.")
add_feedback("customers.signup_date", "say what event this timestamps, not that it is a date")
add_feedback("orders.total_amount", "must state that this is in INR and includes tax")

# --- iteration 2 -----------------------------------------------------------
generate()          # v2 — the three above change, everything else holds
show()

# Widen one: apply to every column in `orders`.
add_feedback("orders.order_id", "always name the granularity, e.g. 'one row per ...'", scope="table")

# And one that applies everywhere.
add_feedback("products.sku", "never start a description with 'This column'", scope="all")

# --- iteration 3 -----------------------------------------------------------
generate()          # v3 — v1 and v2 feedback are still in force
show()

# Rejected examples: caught before they ever reach a prompt.
add_feedback("products.title", "bad")                  # gate 1: too short
add_feedback("products.title", "make it better plz")   # gate 2: vague

report()
print("\nopen simple/report.html")
