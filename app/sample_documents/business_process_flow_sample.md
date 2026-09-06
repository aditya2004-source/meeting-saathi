# Business Process Flow — Customer Request Handling (AS-IS)

This flow reflects the process as described in the meeting, including the
approval and discount sign-off steps discussed.

```mermaid
flowchart TD
    A[Customer Request Received] --> B[Sales Validates Request in CRM]
    B --> C{Request Complete?}
    C -->|No| D[Ask Customer for Details]
    D --> B
    C -->|Yes| E{Discount Above 15%?}
    E -->|Yes| F[Director Approval]
    E -->|No| G[Manager Approval]
    F --> H[Process Request]
    G --> H
    H --> I[Customer Confirmation]
    I --> J[End]
```

Missing or unclear steps may require human review before this is finalized.
