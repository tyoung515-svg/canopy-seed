# Canopy Seed — What Is This?
*A plain-language guide for anyone who wants to understand what Canopy Seed does, who it's for, and what it can become.*

---

## The Problem Everyone Has With AI

Here's what actually happens when most people try to use AI to build something:

They type a message. The AI produces something. It's either too generic, or wrong, or almost-right-but-not-really. They try rephrasing. They get a slightly different version of the same problem. After a while, they give up and conclude that AI "can't do what I need."

The AI didn't fail. The input failed.

Building software with AI requires precise, structured context — a clear description of what you want, who will use it, what constraints matter, what it should connect to, what it definitely should NOT do. Most people don't know how to write that. They haven't built software before. They don't know the vocabulary. They just have a need.

This is the context gap. It's not a knowledge problem. It's a communication problem.

**Canopy Seed solves the context gap.**

---

## What Canopy Seed Does

Canopy Seed helps you turn an idea into working software through a conversation.

You don't fill out a form. You don't write a specification document. You don't learn to prompt an AI correctly. You just talk — the same way you'd describe a problem to a colleague over coffee.

Canopy (the friendly AI guide at the heart of the system) asks you questions one at a time. It listens to your answers. When it needs to understand something better, it quietly does research in the background — looking up relevant patterns, best practices, and technical context — then incorporates what it learns into the conversation without interrupting you.

When Canopy has gathered enough to understand what you're building, it creates a complete project blueprint: a structured document that describes your project's goals, features, constraints, and architecture in enough detail that a team of AI agents can actually build it.

Then those agents get to work.

---

## The Three Stages of Canopy Seed

### Stage 1: Plant Your Seed

The first screen is a single question: *What would you like to build?*

You can answer in plain language. You can drop in a photo of a whiteboard sketch. You can upload a screenshot of something you want to replicate or improve. You can describe a frustration — "our team keeps forgetting to update the task board" — and let Canopy figure out what that means technically.

You choose which AI model powers the conversation: Claude (best for complex reasoning), Gemini (strong for research-heavy projects), or a local model if you want everything to stay on your computer.

### Stage 2: The Conversation

Canopy asks one question at a time. Short questions. Simple language. It acknowledges what you've said, builds on it, and gently steers toward the things it needs to understand.

Alongside the chat, a sidebar fills in quietly — your project's name, goals, the features being identified, any open questions Canopy still has. You're watching your idea take shape in real time without having to be the one organizing it.

When Canopy needs external information — what technology would best fit your use case, what similar systems have learned, what the standard approach is for the kind of problem you're describing — it pauses briefly to research. You see a small indicator. Thirty seconds later, Canopy has better answers and the conversation continues with more depth. You never had to Google anything.

### Stage 3: Your Project Blueprint

When Canopy has enough, it says so. It presents your project blueprint: a summary of everything discussed, organized into a format that both humans and AI agents can work from.

From here you have three options:

- **Start Building** — hand the blueprint to Canopy Seed's agent swarm, which immediately begins building your project
- **Export Overview** — download a human-readable document (great for sharing with a team, or uploading to NotebookLM for an audio overview)
- **Keep Talking** — go back to the conversation to refine anything

---

## The Growth Model

Here's what makes Canopy Seed fundamentally different from other software tools.

Most software is finished when you first use it. It does what it does. You adapt to it.

Canopy Seed is designed to be unfinished. Intentionally. It starts as a base system and grows into whatever you need it to be, shaped by your actual use, your actual problems, and the tools you actually reach for.

The first time you plant a seed, you might get a simple internal tool — a way to track tasks, or a small data processor, or an automation that handles something you've been doing manually. That's the seed.

Over time, as you describe new needs, Canopy extends what it built. New capabilities are added. Existing ones are refined. The software evolves alongside you, rather than staying fixed at the version that existed when you first installed it.

This is not "AI writing code for you." This is software that has a growth mechanism built into its foundation.

---

## Version History: Undo for Your Software

Every time the agent swarm makes changes to your project, Canopy Seed automatically saves a snapshot of what existed before. It keeps the three most recent snapshots at all times.

If an agent change breaks something, or takes the project in a direction you didn't want, you can roll back to any previous version with a single click. No lost work. No irreversible decisions.

This is what makes it safe to let AI agents modify real code. The fallback is always there.

---

## Who This Is For

**People with ideas who aren't developers.** If you've ever thought "I wish there was a tool that did X" but didn't know how to build it — Canopy Seed is the on-ramp.

**Developers who want to move faster.** The conversation-to-context pipeline replaces the planning and specification phase that usually consumes the first days of a project.

**Teams that have needs their current tools don't meet.** Off-the-shelf software rarely fits exactly. Canopy Seed gives teams the ability to grow custom tools that match their actual workflow rather than conforming to someone else's product roadmap.

**Organizations that want AI integrated thoughtfully.** Canopy Seed's safety model — layered permissions, explicit approval for destructive actions, logged decisions, rollback at every step — means this isn't autonomous chaos. It's governed, auditable, and reversible.

---

## What It's Not

**Canopy Seed is not a chatbot.** Chatbots answer questions. Canopy Seed builds things.

**Canopy Seed is not a no-code tool.** No-code tools give you pre-made blocks to assemble. Canopy Seed produces code that fits exactly what you described, not what a product team pre-decided was useful.

**Canopy Seed is not an AI coding assistant.** Copilot and similar tools help developers write code faster. They assume you already know what to build. Canopy Seed helps you figure out what to build, then builds it.

**Canopy Seed is not an autonomous AI.** The agents that build your project are supervised. Potentially dangerous actions require confirmation. Everything is logged. You can always see what changed and undo it.

---

## A Real-World Example: How a Seed Becomes a Tree

Systematic Power Solutions is a battery and car audio conglomerate — a group of about twelve companies including XS Power and Sundown Audio. Like most mid-sized industrial companies, they use project management software that never quite fits. Teams fill it in reluctantly. Managers can't get a clear picture of what's actually happening.

The CIO planted a seed with Canopy Seed: a simple internal task tracker that employees could update through a conversation rather than a form. The AI would write the update, not the employee.

That seed grew. The task tracker learned to recognize patterns in the updates — blockers that kept coming up, handoffs that kept getting dropped. It started surfacing those insights automatically. Then inventory data was added, and the tracker could see when a task was blocked because of a supply issue rather than a people issue.

Sales teams got tools for quoting and analysis that shifted with their actual needs. The CEO could ask a plain-language question and get an answer drawn from the company's actual operational data, with citations back to the source.

None of that was designed upfront. It grew. Each new need was a new conversation with Canopy. Each conversation produced new capability. The gardeners — the employees — shaped what grew by showing up and using it.

**That's the model. The gardener provides the care and attention. The seed provides everything else.**

---

## The Safety Model

Canopy Seed was designed by someone who thinks about failure modes the way a battery engineer thinks about failure modes — which is to say, seriously.

Every action the system can take is classified:
- **Safe to run automatically** — basic reads, status checks, information retrieval
- **Run after confirmation** — file writes, complex shell commands
- **Require explicit "yes"** — anything destructive, anything that affects system settings
- **Not permitted** — certain categories never, regardless of instruction

Every agent action is logged. Every change creates a snapshot before it's applied. The web fetch system has a domain policy — known-safe domains are allowed, known-risky ones are blocked, everything else requires review.

This isn't security theater. These are real layers that make it safe to hand the system a task and walk away.

---

## Built With AI, Documented Honestly

Canopy Seed was built by AI agents coordinated by a human architect. Four agents, two days, two passes of review.

We document this because we think hiding it is dishonest, and because we think the quality of the output is the only thing that should matter to you. Every architectural decision is explained in `ARCHITECTURE.md`. Every trade-off is logged in `DECISIONS.md`. The agents that touched each file are named.

If you're a developer looking at this code: judge it on what it does, not on how it was made. If it's clean, correct, and well-reasoned — that's what matters.
