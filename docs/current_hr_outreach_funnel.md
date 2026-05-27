# Current HR Outreach Funnel

Canonical order for the controlled CRMchat HR agent.

Global runtime rules:
- Candidate questions are higher priority than scripted sends. If a new question appears before a planned text or voice, answer the question first using LLM + knowledge/template context, then resume the funnel step.
- Do not answer candidate questions with "уточню/вернусь" when the answer exists in knowledge cards or template dialogue.
- Voice notes are queued with about 50 seconds of recording wait before send. During that wait, polling continues; if the candidate asks a question, the queued voice is cancelled, the answer is regenerated, and the voice step is queued again after the answer.
- The controlled runner passes relevant knowledge cards and template dialogue snippets into the LLM context before Brain V2 is fully active.

## Stage 0: lead_created
- Send first-touch template.
- Do not call LLM.
- Move to `waiting_first_reply`.

## Stage 1: waiting_first_reply
- Interested replies: "привет", "да расскажи", "интересно", "послушаю", "что за работа".
- If interested, ask age and move to `age_gate`.
- Refusals: "не интересно", "не пиши", "отстань", "нет спасибо".
- If refused, close lost / do not contact.
- If unclear, ask one short clarification or use cheap classifier.
- If the candidate asks a question while showing interest, answer it first, then ask the age gate question in the same turn.

## Stage 2: age_gate
- If age is 18 or older, save `age` and `is_18_plus=true`, then move to `basic_info_pack`.
- If the age answer also contains a question, answer the question before any voice pack is queued.
- If age is under 18, close lost.
- If candidate avoids the question, softly repeat that it is a formal requirement.

## Stage 3: basic_info_pack
- Send the basic voice pack only when there are no unanswered candidate questions.
- Then send: "Если интересна наша сфера, давай расскажу про зп и график."
- If candidate agrees, move to `qualification_faq` and send the deep info pack.

## Stage 4: qualification_faq
- Enable Router -> RAG -> DialogueBrain.
- Answer candidate questions and objections, then resume agenda.
- Typical topics: nudity/webcam/OnlyFans, stream tasks, platforms, English, foreign audience, equipment, pay, payouts, contract, documents, breaks, contacts of other models, company.

## Stage 5: company
- When questions are closed, explain Profitcast.
- Provide Telegram channel and website.
- If link does not open, resend.
- If candidate asks for socials, provide website and Telegram channel.

## Stage 6: personalization
- Ask: "Расскажи немного о себе: учишься/работаешь? чем любишь заниматься?"
- Extract `work_study`, `availability`, `hobbies`, `creative_background`, `has_channel`.
- Map interests to possible stream themes.

## Stage 7: interview_close
- When candidate is 18+, interested, core objections are closed, and basic personalization exists, offer interview.

## Stage 8: pre_schedule_questions
- Ask room/privacy and device model before booking.
- Then collect contacts.

## Stage 9: contact_collection
- Collect `contact_name`, `contact_phone`, `equipment`, and `photo_received` only if the process requires it.
- Personal photos and phone numbers must be stored only in lead profile slots, not knowledge cards.

## Stage 10: scheduling
- Ask if tomorrow works.
- Offer 11:00-18:00 MSK.
- If timezone differs, collect region and confirm both MSK and local time.

## Stage 11: confirmation
- Confirm name, date, MSK time, local time if different, Zoom format, and support availability.

## Stage 12: post_schedule_support
- If Zoom/link issue happens, use `support.zoom_link_download_appstore`.
- Give zoom.us/download and App Store path.
- Ask if it worked.
- If not, hand off to a human.
