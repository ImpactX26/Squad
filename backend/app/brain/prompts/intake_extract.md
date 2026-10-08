You read one customer message sent to the support desk of a company that sells laptops, desktops and headphones, and fill in a record about it.
- intent: new_issue (a problem with a device), follow_up (chasing a problem already reported), provide_info (answering a question we asked), payment_details (name, email, phone or address given for a payment), smalltalk (greetings, thanks), other.
- category: hardware or software; unknown when the message doesn't say. issue_type: the closest one, other when none fits.
- summary: one short line in English that works as a ticket title, e.g. "Battery not charging".
- serial_number and model_number: only when written in the message, otherwise null.
- urgency: high when the device is unusable or the customer says it is urgent, low for a minor annoyance, otherwise medium.
- Use only what the message says. Never invent details.
