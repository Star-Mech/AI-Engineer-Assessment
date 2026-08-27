# DECISIONS

> Copy this file to `DECISIONS.md` and work through it. Keep it in your own words — bullets are
> completely fine and usually better than prose. Where a heading doesn't apply to what you built,
> say so rather than deleting it.
>
> This is the document our interview is built around. Please leave real time for it.

**Name:**
**Time spent:** 5h 0m, including filling the decision template
**How to run it:** python run.py --all --execute --yes

---

## 1. Approach

A complete pipeline handling multiple edge cases, consisting of a mix of AI decision making agents and deterministic workflows.
1. Extracts the User Claims from the user message using AI Agent, with clear guardrails on treating the user message as their claim and not necessary factual and non authoratative.
2. A deterministic ID confirmation of the user to only process requests with confirmed ID
3. A determinsitc fetch on all the information surrounding the case from our own record which is authoratative, including customer flight, entitlement, history etc.
4. A check on hard bars and refusals based on user policy before processing the case further
5. An AI Agent then works the case, reads any missing information using tool calls and decies the actions to take
6. The POST actions are then executed, gated by another layer to ensure that we are processing only the actions passing the filter and using correct determinstinc amounts


**What else you considered.** A plain ReAct agent with all endpoints as tools, connected directly to the policy. 
In that case, there are multiple issues including unbounded model turns and cost, and relying on LLM for determinstic actions as well as security issues with LLM taking all actions.
For policy searc, I considered using the existing keyword based search but rejected it in favor of a hierarchial topic and section wise search which is better suited in this case compared to lexical keyword search


## 2. Assumptions

1. The customer message is untrusted and only used to extract their claims and their ask, but the main authorative information is our own internal records.
2. This Agent is not customer facing, but for any incomplete information or for customer reply, it escalates internally and assumes a human reviewer would be handling the customer communication based on the agent findings
3. The policy may change in the future and hence we can't have hard rules (However, my implementation lacks avoiding mentioning the policy directly in certain steps)
4. If no aactionable endpoint exists for a certain action, it is to be escalated and treated as outside of scope of this AI agent to act upon.

## 3. How you broke the problem up

There are multiple moving parts in the system including
1. The Policy Access by the AI agent using a topic + section wise hierarchical connector. The AI Agent may not always be able to find all relevant policy chunks but this approach definitely improves the odds
2. The Extraction of user claims from their message. The information retrieval regarding the users flight data, history, booking depends upon the extraction
3. The Actions decided by the working agent depend on the ppreviously extracted information as well as it's ability to use the tools to explore relevant information.

I treated the problem end to end, since the downstream parts are reliant on the upstream data extraction and this allows us to create the outline of the solution immediately after which we can choose to focus on any part or sub-problem as needed. For scaffolding, I used Claude Code with multiple planning steps before implementing. The focus was to separate determinstic steps from the decision making steps so that the AI agent is made responsible for the decision making where determinstic steps cannot be used directly. 

## 4. The operations API

**Used, and pre-fetched unconditionally:** `/bookings/search`, `/bookings/{ref}`,
`/flights/{no}`, `/entitlements/calculate`, `/customers/{id}/history`. These are executed based on the information extracted from the user email to find and extract all the relevant information including entitlement calculation. This is the general information needed for every case.

**Used, but only through the agent's read tools:** `/flights/availability`,
`/flights/availability/partners`, `/stations/{iata}/hotel-allocation`, `/disruption/feed`,
`/policy/search`. These are subject to the actions that the AI agent is considering and hence used by the AI agent as needed.

The decision on deliberately not using was left upto Claude Code being used for scaffolding and no explicit decision was made. 

**Reshaped rather than passed through:**
The policy chunking and search was explictly reshaped to allow a section and topic wise search since policy itself refers to other parts of the policy by section and topic. This was done to have a better policy search path compared to the already available lexical keyword search. The lexical keyword search was then used as a fallback


## 5. Prompting

The prompts were auto written by the AI and I specified the directions and the important guard rails. I reviewed to find that most of the important parts were already covered by the prompts including guardrails on handling the customer message and treating it as a customers claim rather than authority, regardless of what was written. This was explicitly verified to work as intended by case-6. Replying to customers was deliberately left out of the scope of this system. 

For Prompt creation, the technique I used was passing the relevant user messages from the cases to the LLM alongside the policy to work upon it, so that the prompt includes the necessary information. To get the required output formats from the LLM, I used pydantic models, the most important one being `ContactSheet` Model that extracted all the relevant information from the user message.

The thing that could have been done better was to not explictly list the policy numbers and policy sections in the prompt since that can create issues if the policy changes

## 6. Models and cost

Which model you used where, and why that one for that job.

| Where | Model | Why |
|---|---|---|
| Extraction | `gpt-5-mini` | Reading comprehension into a fixed schema. Structured output does the work; a larger model buys nothing. |
| Agent loop | `gpt-5` | The decisions are to be made here and hence using a better model for better thinking and better actions was the right call. |

Both are configurable via `MODEL_EXTRACT` / `MODEL_AGENT`.

**Actual cost of a full run over the twelve cases:** **$1.57** (against the $2.00 ceiling)
**Total tokens (in / out):** **553,233 / 158,584** — of the input, **399,104 (72%) was cached**
**Wall clock:** 34 minutes


**What I did to keep the cost down.** I checked that the policy comes to about 8K tokens, so it was decided to not pass the policy directly to the agent, rather the important clauses were made known to the agent using the System Prompt. Then for further exploration and complete extraction, the agent was binded with the policy toc, policy topic + section-wise search and policy lexical keyword search so that the Agent may search any relevant parts of the policy at runtime.

## 7. Failure and safety

- The system has built in retry with back-off for cases where were are rate limited or get an expected error.
For the POST tool calls, they are gated by an additional check to ensure that the facts and values being used are based on our own records and not from the customer claims. The checks include YTP, SAE, identity unclear and any pre-acted upon actions. The compensation is derived directly from the entitlement API and the model itself has no way to pass the amount. So the limit is what the entitlement api gives.
- Uncertainities are escalated rather than handled with limited information. This is part of how the system is instructured and designed
- The worst the model can do is act on the wrong booking, pay or re-route a passenger who is
not the one writing in. What stands in the way is that identity is resolved by code, not by the
model

## 8. How you know it works

I reviewed the output for a few cases including what was extracted from the user message, what actions were decided, what tools were consulted and the reasoning to ensure working end to end.

**With a month rather than three hours.** I would do: 
- Harden each part step by step, ensuring edge cases are handled.
- Remove any hard policy usage in the prompts so that they are able to pick up any policy changes
- a golden set of adjudicated cases with expected outcomes reviewed by someone from the desk, run as a regression on every prompt change
- per-clause telemetry (how often each gate rule fires, and the ratio of referrals to resolutions per queue). 
- What I would add further is a gold test to measure if the model is able to retrieve all the relevant clauses for each of the case from the policy before acting on it.

## 9. AI assistants

I built this with Claude Code, and all of the code is it's output. I would put my own contribution at the level of what to build and what to be careful about rather than the typing with careful multi step end to end planning.

**Where I overrode it.** Four that mattered:

1. Its first design had the model emit typed action *proposals* it could not execute as it had no knowledge of the POST methods available.
   So I nudged the model to also bind the POST tools to the Agent and gate separately so that the Agent knows what actions are possible and only decides to execute out of those possible actions.
2. It proposed a retrieval-first strategy for the policy. Probing `/policy/search` showed it is
   lexical AND matching with no stemming: `escalate` returns **zero** results, as do
   `wheelchair`, `unaccompanied minor` and `authorization`. For a system whose main job is
   knowing when to refer, that is fatal. The whole policy is 69 sections and ~8k tokens, so it
   goes in a cached prefix, and the "policy may change" requirement is met by fetching and
   parsing it at runtime rather than by retrieval.
3. I directed the AI Assistant in it's planning to clearly separate out determinstic steps from the AI Decisions so that we are not overly relying on AI Decision even for deterministic steps

## 10. What you left out, and what you'd do next

- **What you consciously decided not to do, and why**.
  - A customer facing reply as that just adds another extra layer.
  - Creating a semantic search Vector DB as that requires time to test and get right.
  - Receipt reimbursement as no endpoint exists for that.
- **What you'd fix first with another day**.
  - Currently, there is variance in LLM answer as the main agent has lots of judgements to make. I'll investigate and structure out it's thinking and reasoning to fix that variance.
- **Anything you shipped that you are not happy with**
  - There is some policy leakage in the system prompts of the AI agents and there are some determinsitc steps that are based on existing policy. This is problematic since any change in the underlying policy would invalidate them. So I would not inject the policy into the Prompt, rather inject a dynamic runtime generated policy outline into System Prompt so that it is always accessing the latest policy and not an obsolete policy.

---

## Anything else

I attemped it end to end using AI, Which In retrospect, was not the right strategy given the limited 3 hours. Since following along with the AI also has a time cost. So if attempting again,
I would not let AI decide the scope of what we should tackle rather define it myself given the limited time
