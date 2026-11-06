# Ticket sale business rules

- A ticket sale requires a payment authorization. Don't remove the payment call.
- A customer must never be left holding an authorization without a ticket.
- Never sell more tickets than are in stock.
- If tickets are available, a buyer with a valid payment must be able to buy one.
- Don't hold a database transaction across an `await`.
