# Weekly best deals

Enable **Weekly best deals recap** under dashboard alert controls and save.
It defaults off and is independent of instant alerts and the existing short digest.
The worker queues a recap on Mondays at 15:00 UTC, or on its next run that week.
Quiet hours, pause controls, verified subscriptions and unsubscribe still apply.

Each recap covers the previous Monday-to-Monday UTC week and includes up to ten
routes, sorted by lowest fare, with one cheapest observed itinerary per route.
Eligible observations are saved independently of instant-alert qualification and
destination cooldowns, so a $313 alert followed by a qualifying $288 fare yields
$288 in the recap.
The recap also includes the best observed fare when no fare met the instant-alert
discount threshold. It retains that week's cheapest price even if its travel date
has passed by publication. Baseline and discount are preserved for qualifying
deals; other observations show the price without an invented discount comparison.
Data collection starts after this upgrade; existing historical observations are
not backfilled.

These are historical observed fares, clearly labeled as such. They are not
revalidated or presented as guaranteed available fares. Empty weeks send nothing.
Missed older weeks are not replayed. Recaps expire before the next scheduled
edition. Durable weekly reservations prevent repeated worker ticks/restarts from
queueing another copy; SMTP ambiguous acknowledgments can still cause a retry
to duplicate a weekly recap. Immediate destination alerts retain uncertain-send
reservations to prevent retry spam. Upgrading preserves subscriptions.
