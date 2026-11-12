# Using Flight-Alerts

Start with the [setup guide](getting-started.md) if the website is not running
yet. This guide describes the dashboard that ships with the project.

## Sign in

Open the HTTPS address configured in `.env`. Your browser asks for the
administrator username and password. These are the credentials you created,
not a GitHub or flight-provider login. Only the administrator uses the dashboard;
recipients only need their invitation and unsubscribe links.

The dashboard uses browser HTTP Basic authentication and has no separate sign-out
button. A browser may keep that login until its authenticated browsing session
ends. Use a private browser session on a shared computer and close it afterward.

## Dashboard tabs

The dashboard opens on **Deals**, showing current flight deals and how they are
selected. Choose **Admin** for delivery settings, recipients, monitored airports,
flight status watches, provider health, scan jobs, and delivery history. The
**Queue a scan** button is also in Admin. Both tabs require the same administrator
login. After submitting an Admin form, you return to Admin with a status message.

## Confirm a recipient

1. Open **Admin** and find **Recipient subscriptions**.
2. Enter your own address and choose **Send invitation** for the first trial.
3. Open the email, follow its confirmation link, and submit the confirmation form.
4. Reload the dashboard. Expect **Confirmed** and **Enabled** for that recipient.

An unconfirmed recipient gets invitations but not ordinary fare/status alerts.
Confirmation links expire after 48 hours; use **Resend invitation** if needed.
**Disable** stops future queued delivery for that recipient. An unsubscribe link
also disables the recipient, and an old confirmation link cannot re-enable an
unsubscribed address. The current dashboard intentionally does not override that
choice or offer a re-enable button.

## Set the delivery rules

In **Admin**, find **Alert and delivery controls**, change settings, then choose
**Save delivery settings**.
Reload the page if another tab has already saved newer settings.

| Setting | Meaning and example |
|---|---|
| Alert minimum discount (%) | How far below the selected reference price a fare must be. A $250 fare against a $1000 reference is 75% below it. |
| Delivery maximum fare age (hours) | How long an observed fare is usable; old or past-departure fares are excluded. Queued fares are checked again before sending. |
| Scan interval (hours) | How often the worker schedules searches. The starter guide uses 24 hours. |
| Pause alerts | Stops new ordinary fare alerts and delays queued fare/status alerts. Scans and invitation emails can still run. |
| Use quiet hours | Delays ordinary alerts during the chosen local-time window. For 10 p.m. to 7 a.m., use start 22, end 7. Equal start/end disables the quiet window. |
| Timezone | A timezone name such as `America/Denver`; not an abbreviation such as MST. |
| Delay destination summaries | Waits the configured interval after a scan completes. Each destination still receives its own email with date/price options. |
| Summary delay (hours) | Must be shorter than fare freshness. For example, a 6-hour delay with 24-hour freshness is valid. |

Each scan queues at most one email per destination after its queries stop. A live
email sent at the same or a cheaper price in the preceding seven days suppresses
that destination. The maximum is two emails per destination per recipient in seven
days, spaced at least 72 hours apart. See [summary and baseline details](scan-summaries.md).

Quiet hours, pending work, quotas, and provider freshness mean mail timing is not
an exact appointment. A fare may expire while waiting and then be canceled.
For a complete stop of scans and delivery, stop the worker from the terminal:

```bash
docker compose stop flight_worker
```

Start it again with `docker compose up -d flight_worker`. Queued work survives a
restart; provider quotas still apply. Review recovered pending mail after a
backup restore because its previous delivery may be uncertain.

## Add destinations

In **Admin**, under **Monitored airports**, enter the destination's three-letter IATA airport code, city,
and two-letter country code. For example, London Heathrow is `LHR` in `GB`.
Choose **Add or update** to save it. A blocklist entry matches an exact airport
code or city, ignoring case; it is not a country or substring filter.

The current airport form provides reference prices for **SLC** and **LAS**.
These are operator-entered assumptions, not a verified market average. Adding
an airport does not add another departure airport. If you chose another origin
in your first configuration, its custom reference must be managed outside this
form; saving the form writes the displayed SLC/LAS reference mapping.

Departure airports, detailed travel-date sampling and provider request limits
come from the first configuration and do not have dashboard controls in this
version. Set them carefully before the initial migration. For changes afterward,
use a reviewed configuration update with the maintainer; editing the bootstrap
YAML alone will not update the database, and deleting the database loses data.

Adding a US destination through this form also adds it to the domestic-airport
allowlist. Removing a destination stops new monitoring for it; removing all
monitored airports produces no new fare searches.

## Queue a scan and understand the result

In **Admin**, **Queue a scan** saves a job for the worker. It does not search inside the browser
request. The worker also creates scheduled jobs on its configured interval.
An already queued/running scan or the manual cooldown can prevent another job.

| What you see | What it tells you |
|---|---|
| Provider health has a recent success | That provider returned a usable response, possibly with no offers |
| A scan succeeded | Its search windows completed with the required provider coverage |
| A scan failed / is incomplete | Check its safe error category and the provider table; some routes may still have results |
| Matching fares has entries | Latest itinerary observations at least 50% below a covered historical cheapest-fare reference; delivery freshness does not filter this list |
| Latest reserved alerts has an entry | A notification intent was saved; it does not prove everyone received it |
| Per-recipient delivery says `sent` | The configured SMTP server accepted the message; inbox placement is still up to the mail service |

The worker uses provider requests even while alerts are paused. Missing keys
disable providers. More origins, destinations and travel-date samples mean more
searches, and retries/rechecks also consume allowance. Keep limits below the
purchased plan. A provider can require additional permissions or lack coverage
for a route even when its key is valid.

The dashboard fare list uses only recorded route history, with no provider or
configured-baseline fallback. It shows at most 12 upcoming itineraries, using
only the latest observation for each itinerary within the rolling 90-day window.
The route reference takes a median of daily medians of the cheapest valid USD
fares for each upcoming travel-date pair. It requires minimum observation-day,
date-pair and sample coverage, and excludes past departures and future observations.
It displays the reference, discount, observation time and deduplicated sample count; a displayed fare is not a guarantee
of current availability. A route with no qualifying discount has no entries.
The dashboard's fixed 50% filter is independent of the alert threshold and delivery
age settings.

The reference used for an alert may come from provider typical-price data,
sufficient historical observations, or your configured reference. Read the
**Baseline source** shown with the alert before interpreting the percentage.
Search fares are not reservations, and booking details or prices may change.

## Send a controlled test

**Send invitation** tests the configured mail connection with your chosen address.
Do this with your own mailbox and confirm it first.

**Queue latest alert for confirmed recipients** is a real resend action, not a
fake test message. It needs an existing reserved alert and queues it for **all**
confirmed, enabled recipients. It still follows pause, quiet hours, freshness,
revalidation and other delivery controls. It may fail or be canceled if that
fare is no longer available. Use it only when every affected recipient should
receive that message. It does not create a qualifying fare when there is none.

## Watch a specific flight (optional)

Flight-status watches need a configured Aviationstack key and an eligible account.
In **Admin**, under **Flight status watches**, enter a flight number such as `DL123` and its departure
date, then choose **Watch flight**. The first successful observation sets a
starting point; later status/delay/gate changes can create alerts for confirmed
recipients. **Stop watching** removes the watch.

Future watches wait until their flight date becomes eligible for polling. The
worker checks watches around the current date, not arbitrary future timetables.
A blank/unknown field means the provider did not supply that information. Always
check the airline for current travel decisions.

## What persists and what changes it

| Item | Where it lives | How to change it |
|---|---|---|
| Website hostname and SMTP connection settings | `.env` on the server | Edit privately, then recreate affected services with Compose |
| API keys, mail password, administrator hash and signing key | Private `secrets/` files | Follow the credential maintenance instructions; recreate affected services |
| Delivery settings, destinations and subscriptions | SQLite in the Docker named volume | Use dashboard controls where available |
| Bootstrap YAML | The private file used for the first migration | Seeds settings once; later file edits are not automatic settings updates |

Use [troubleshooting](troubleshooting.md) for common failures and
[operations](operations.md) for backups and updates. Restarting containers keeps
data. Deleting the database volume removes settings, consent state and history.
