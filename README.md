# Deadline Delta

**Which deadline applies to me?** A scholarship revision can list different cutoffs for first-time applicants and renewals. Deadline Delta compares two notices, finds the date for the applicant group or submission route you choose, and shows the exact line that supports it. If the text gives no date or competing dates for that scope, it asks for confirmation. It also shows changes in required documents, where to submit, and who can apply.

## Try it

Open `public/index.html` to explore four fictional examples. They run locally and are labeled as examples. The published demo's **Compare with Clocked** button runs each example or your own pasted notices through the live API.

| Try this example | What to look for |
| --- | --- |
| **Different groups · EN** | Select **First-time applicants** for 20 November or **Renewal applicants** for 27 November. The source text stays the same; the applicable line changes. Without a selected group, no revised date is promoted. |
| **Date pending · DE** | The old date stays visible for reference. The new notice says the deadline will be announced later, so the app asks for confirmation. |
| **Date moved · EN** | The new date is 25 November instead of 18 November, and a recommendation letter is explicitly no longer required. |
| **Two cutoffs · EN** | Two lines in the revision give different dates. The app shows both lines and selects neither date. |

Click a **Line** button to locate the wording in the notice. Use **EN / DE** to change the interface. To test the real integration, select **Renewal applicants**, run **Compare with Clocked**, and look for **Live analysis · Clocked** and the quote on line 3. Then switch to **First-time applicants** and run it again. For a real notice, paste both versions and optionally enter the group or submission route as written in the notice.

## How Clocked is used

- **Compare** flags possible conflicts and overlaps across the two notices.
- **Extract** locates the deadline clauses and exact excerpts for three other fields in each notice. The app checks the wording against the original and displays the source line. It compares instants only when dates, times, and explicit time zones are available.
- **Rerank** runs when a scope is supplied and there are multiple cutoff clauses. Only source passages marked `use` are considered. A strong score by itself cannot select a date, and competing applicable cutoffs go to review.
- **Verify** (`grounding` in the SDK) checks the candidate date and its scope against the full revised notice. For a scoped result, it checks the proposed supporting quote as part of the claim. It sees only the revision, so an old date cannot establish a current cutoff. A supported result shows the date in the top card; uncertainty keeps it in review.

The browser talks to `/api` on the same site. The server reads `CLOCKED_API_KEY` from its environment. Enter only public notices and a group label in the live demo; the texts and label are sent to Clocked for processing. No account is needed to use the app. It has no database.

## Deploy

Import a public GitHub repository containing these files into Vercel with framework preset **Other**. `vercel.json` serves the `public` directory and `api/index.py` provides the Python endpoint. Add `CLOCKED_API_KEY` to the project environment before deploying. For a walkthrough in Ukrainian, see [START-UA.md](START-UA.md).

Run `python3 -m unittest discover -s tests -v` to check the logic with simulated Clocked responses. Tests cover separate group cutoffs, missing or irrelevant clauses, multiple applicable dates, and a Verify refusal. Before submitting the demo, run **Different groups · EN** with **Renewal applicants** and **Two cutoffs · EN** with the group field blank; confirm that the former shows its group-specific date and the latter asks for confirmation.
