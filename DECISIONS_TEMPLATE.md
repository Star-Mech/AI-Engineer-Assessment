# DECISIONS

> Copy this file to `DECISIONS.md` and work through it. Keep it in your own words — bullets are
> completely fine and usually better than prose. Where a heading doesn't apply to what you built,
> say so rather than deleting it.
>
> This is the document our interview is built around. Please leave real time for it.

**Name:**
**Time spent:** (roughly, and honestly)
**How to run it:** (the one command)

---

## 1. Approach

What you built, in a few sentences — enough that we can picture it before we read the code.

**What else you considered.** Name at least one approach you seriously thought about and rejected,
and say what made you reject it.

## 2. Assumptions

Things the brief or the cases didn't settle, where you had to make a call. For each: what you
assumed, and why that rather than the alternative.

This section is genuinely useful to us — the exercise has ambiguity in it deliberately, and how
people resolve ambiguity is most of the job.

## 3. How you broke the problem up

What the moving parts of your system are and what each is responsible for.

Why that division and not a different one — or why no division, if you kept it as one piece. What
does your split buy you, and what does it cost you?

## 4. The operations API

Which parts of it you used, and how you presented them to your system.

Which parts you deliberately did **not** use, and why. (Leaving things out is a decision, not an
omission — we'd like to hear the reasoning.)

Anything you reshaped rather than passing through as-is: merged, split, filtered, summarised,
renamed. What drove that?

## 5. Prompting

The decisions you actually spent time on. For example:

- what you told the system about its job, and what you deliberately left out;
- anything you tried that made things worse and had to remove;
- how you handled instructions or claims contained in the material it reads;
- how you got it to produce the record format you wanted.

Quote the specific lines you'd defend. If one part of a prompt took you three attempts, that's the
interesting one.

## 6. Models and cost

Which model you used where, and why that one for that job.

| Where | Model | Why |
|---|---|---|
|  |  |  |

**Actual cost of a full run over the twelve cases:** $
**Total tokens (in / out):**
**How you measured it:**

If you did anything specific to keep the cost down, say what — and what it cost you in quality.

## 7. Failure and safety

- What happens when something the system depends on doesn't respond, or responds badly?
- What happens when it isn't sure?
- What stops it doing something expensive, wrong or irreversible?
- What is the worst thing your system could do if it got a case badly wrong, and what stands in
  the way of that happening?

## 8. How you know it works

What you built to check it, what that check actually tells you, and where it is blind.

**And with a month rather than three hours:** how would you know this was working in production,
and how would you find out it had stopped?

## 9. AI assistants

Where you used them, and roughly how much of the result is theirs.

**More interesting: where did you override them?** Somewhere one of these tools suggested something
and you didn't take it. What was it, and what was your reasoning?

## 10. What you left out, and what you'd do next

- What you consciously decided not to do, and why.
- What you'd fix first with another day.
- Anything you shipped that you are not happy with. (Saying so scores better than hiding it. We
  will find it anyway, and "I know, here's why" is a much better conversation.)

---

## Anything else

Anything you want to tell us that the headings above didn't ask for.
