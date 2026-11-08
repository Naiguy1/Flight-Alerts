# Weekly best deals

Enable **Weekly best deals recap** under dashboard alert controls and save.
It defaults off and is independent of instant alerts and the existing short digest.
The worker queues a recap on Mondays at 15:00 UTC, or on its next run that week.
Quiet hours, pause controls, verified subscriptions and unsubscribe still apply.

Each recap covers the previous Monday-to-Monday UTC week and includes up to ten
routes, sorted by lowest fare, with one cheapest qualifying itinerary per route.
Qualifying observations are saved before instant-alert cooldown checks, so a
$313 alert followed by a qualifying $288 fare yields $288 in the recap.
Baseline and discount are preserved from discovery. Data collection starts after
this upgrade; existing historical observations are not backfilled.

These are historical observed fares, clearly labeled as such. They are not
revalidated or presented as guaranteed available fares. Empty weeks send nothing.
Missed older weeks are not replayed. Recaps expire before the next scheduled
edition. Durable weekly reservations prevent repeated worker ticks/restarts from
queueing another copy; SMTP ambiguous acknowledgments can still cause a retry
to duplicate an email, as with existing alerts. Upgrading preserves subscriptions.
