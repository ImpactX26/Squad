A support agent sent a customer a numbered list of troubleshooting steps and asked which worked, which didn't, and which they skipped. Read the customer's reply and say, for each step they mention, what happened.

Rules:
- `step_number` is the number from the list. A step named by its words ("the BIOS one") is the number of the step with those words.
- `result` is exactly one of: `worked`, `failed` (didn't work, didn't help, no change), `skipped` (didn't try, couldn't do it).
- `note` is the customer's own words about that step, copied exactly, or null. Never write your own words there.
- Leave out every step the customer does not mention. Never guess.
- `problem_fixed` is true only when the customer says the problem itself is now gone.