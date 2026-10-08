"""UPI payments (ARCHITECTURE.md §7.6): the customer pays the company's UPI ID from a QR, submits the
12-digit UTR, and the backend matches it against the bank's credit SMS forwarded into the project Gmail.

    money.py         Decimal rupees, Indian formatting, IST, the upi:// link
    details.py       the booking contact details, validated in code
    invoice.py       one view of a payment for the pay page, the emails and the chat replies
    upi_verifier.py  the bank-alert poller, parser, anti-spoofing checks and the matcher
"""