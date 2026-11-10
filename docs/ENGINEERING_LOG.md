# Engineering log

Real incidents from running the system, in the order they were handled. Each entry gives the symptom, how it was found, the root cause, the fix and the test that keeps it fixed. Website names are left out on purpose.

## 1. Only 3 of about 410 pages passed in a week

**Symptom.** In its first full week on the strict rules, the system opened about 410 pages and only 3 passed every check. The target was two a day for each of three websites.

**How it was found.** I counted every rejection by reason and by day, then reopened the pages that had been refused and read what was really on them. Of the 410 pages, 241 were not guidelines pages at all (ordinary articles, "sites that accept guest posts" lists, job adverts), and 169 were real. Of those 169, an estimated 25 to 35 had been refused wrongly.

**Root causes and fixes**, each in its own entry below: the search ignored quotation marks (entry 2), the email-address rule was too strict (entry 3), a login box was read as "an account is needed" (entry 4), and a one-off re-check wasted checks (entry 5).

## 2. The search returned the wrong kind of page

**Symptom.** Searches for a quoted phrase such as "write for us" returned articles that merely discuss the subject.

**Root cause.** The search service treats quotation marks loosely unless told otherwise.

**Fix.** Queries that contain a quoted phrase now set the service's documented `exact_match` option. It costs the same per search.

**Test.** `test_the_search_asks_tavily_for_the_exact_phrase`.

## 3. Good addresses refused as "not offered for guest posts"

**Symptom.** Guidelines such as "Please send the post to name@site" were refused, because the sentence did not contain a word like "submit". Some sites wrote the address out ("name [at] site [dot] com") to keep robots away.

**Root cause.** The rule only accepted an address next to an invitation to submit, and only read addresses that were written normally.

**Fix.** An address now counts when it sits next to an invitation, or inside the guidelines page's own content (never the header, menu, sidebar or footer), or has a name that says it is for submissions, such as `guestblogger@` or `editor@`. Written-out addresses are decoded. A footer contact still never counts.

**Tests.** `test_an_address_in_the_guidelines_themselves_or_named_for_submissions_counts_but_the_header_and_footer_never_do`, `test_addresses_written_out_in_words_are_found`.

## 4. A menu login box meant "account required"

**Symptom.** Pages that take article submissions by email were refused as needing an account.

**Root cause.** Any password field on the page set the "account required" flag, and many blogs show a sign-in box in the menu or a pop-up on every page. A comment box's note "you must be logged in to post a comment" matched too.

**Fix.** A password box inside the header, menu, footer or a pop-up, or a plain sign-in form with "remember me", is ignored. The wording rule no longer matches comment-box notes. A page that really says you must log in to submit an article is still refused.

**Test.** `test_a_menu_login_box_does_not_mean_an_account_is_needed_but_saying_so_does`.

## 5. A one-off re-check wasted a quarter of the daily checks

**Symptom.** About 10 of each website's 25 daily checks were spent on pages already checked the day before.

**How it was found.** The same domains kept appearing in consecutive days' reports.

**Root cause.** A one-off re-check, added when a new route went live, selected every page turned down for "no form". It had no date limit, so it also re-queued pages that were turned down after the new route existed.

**Fix.** One-off re-checks only touch pages turned down before the rule they correct.

**Tests.** `test_a_page_turned_down_after_the_email_route_existed_is_not_checked_a_second_time`, `test_guidelines_pages_turned_down_under_the_old_rules_are_checked_once_more`.

## 6. A pitch was reported as published, and then as an error

**Symptom.** The dashboard showed "verification failed: no link on the page" for a pitch that had never been published.

**Root cause.** The tracker finds a published article by matching words of its title against the website's sitemap. A pitch is titled "Pitch to <website name>", and three of those words also appeared in an unrelated article's address.

**Fix.** Pitches are not looked for in the sitemap, because a pitch has no article title. The website's own name words never count as a match.

**Test.** `test_a_pitch_is_never_matched_to_an_unrelated_article_of_the_website`.

## 7. A form that emptied itself after it was filled

**Symptom.** "It fills the form, and then it all disappears."

**How it was found.** I reproduced it in headless Chromium and watched the form. The website's own form plugin reset it about two seconds after the extension had filled it, before the person answered the quiz question.

**First fix, and the lesson.** I refilled the form after a delay and tested it by injecting the script into the page. The owner still saw it fail, because a real Chrome extension runs in an isolated world and my test did not. I then launched Chromium with the actual extension loaded and ran the real page.

**Fix.** The extension refills at the moment of the form's `reset` event and checks every 250 ms. It only refills boxes that are empty, so it never overwrites what a person typed. The automatic sender waits until the values have stayed put before it presses Send. In the real browser the box was empty for about 15 ms and stayed filled for the 30 seconds I watched.

**Tests.** The extension checks in `extension/test_extension.js`, and `test_the_real_browser_refills_a_form_the_website_wipes_and_sends_it_full`, which fails without the fix.

## 8. The first model outage stopped the whole pass

**Symptom.** On busy mornings the first failed model call ended the check pass, and the pile of unchecked pages grew to 55.

**Root cause.** A model failure was treated as the end of the pass.

**Fix.** After the first failure a stub client answers "busy" instantly. The pass continues with the deterministic checks, and only the pages that need the model wait. A per-run time budget stops long waiting.

**Test.** `test_when_gemini_is_down_the_pass_carries_on_with_the_system_s_own_checks`, which fails on the old code.

## 9. Honesty rules that reading pages by hand exposed

**Symptom.** When I checked a day's results by hand, I found pitches that should not have been sent: one went to a publication whose guidelines are "members only", and several sites forbade AI-written text.

**Root cause.** The checks looked for permission signals but not for explicit prohibitions.

**Fix.** Wording rules now refuse websites that forbid or limit AI-written text, ask link builders not to apply, or publish only their own members. Each pattern was tested against ordinary sentences (such as "write about AI tools for authors") so it does not refuse good pages, and against 85 real queued pages, where it refused 7. I read the matching sentence in each of those 7 by eye.

**Tests.** The parametrised tests for the AI, link-builder and members-only rules.

## What these have in common

The expensive mistakes were not model mistakes. They were quiet ones in the plumbing around the model: a date missing from a one-off job, a match that was too loose, a test that did not run the real artefact. The fixes that lasted came from measuring first, reproducing the failure, and adding a test that fails on the old behaviour.
