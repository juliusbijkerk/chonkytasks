# AI Workflow Discovery Tool
## Project description

We want to build an **AI workflow discovery tool that observes how someone works and identifies practical opportunities for automation**.

Many companies know that AI could improve their operations, but employees and managers often do not know what current models can do, which tasks are suitable for automation, or how to translate their everyday work into an automation project. Our product would help bridge that gap by learning from the work itself.

The long-term experience would be straightforward: an employee enables a background application during their working week. The application captures their on-screen activity and analyzes recurring workflows. At the end of the week, it presents a personalized report explaining which tasks could be automated or assisted by AI, how those automations would work, and what would be required to implement them.

**The selling point is: “Work as usual. Discover where AI could save you time.”**

Instead of asking employees to document their processes or understand AI capabilities, the product identifies opportunities from observed behavior. Each recommendation should reference concrete examples from the recording, making it understandable and verifiable.

Potential users include operations teams, administrative staff, customer support teams, and small businesses that lack dedicated automation expertise. For example, the system might observe an employee repeatedly reading customer requests, extracting information into a spreadsheet, and drafting similar responses. It could suggest a workflow that extracts the relevant fields, updates the spreadsheet, and prepares a response for human review.

## Hackathon prototype

For the hackathon, we want to build a working demonstration within approximately ten hours.

The prototype will accept an uploaded screen recording of approximately five minutes in which someone performs a repetitive business task several times. It will analyze the recording, reconstruct the workflow, identify the repetition, and produce a specific automation recommendation.

The intended output includes:

- A timeline of the main steps performed, linked to timestamps and screenshots.
- A description of the recurring task and its observed occurrences.
- An explanation of which steps could be automated and where AI would help.
- A proposed automation workflow, including required integrations and remaining human decisions.
- Observed task durations, with any projected savings clearly separated from measured results.

Automatically implementing or executing the recommended automation would be a possible extension. The core demo is complete when it can accurately explain the observed workflow and propose a credible improvement.

## Proposed technical approach

The proposed technical approach is a pipeline combining video processing and multimodal language models:

1. **Extract and select screenshots.** Sample frames from the recording, retain timestamps, and use visual change detection to select useful states. Remove consecutive near-duplicates while preserving repeated visits to the same screen, since these may indicate recurring work.

2. **Reconstruct the activity.** Send ordered screenshots with timestamps to a vision-capable Mistral model, such as Mistral Large 4. Ask it to identify visible applications, workflow steps, changes between states, and likely actions. Return structured data with supporting screenshot references and explicit uncertainty.

3. **Identify recurring workflows.** Analyze the reconstructed sequence to find repeated patterns, even when the customer names, documents, or values change between occurrences.

4. **Generate automation proposals.** Map the recurring tasks to capabilities such as information extraction, classification, drafting, deterministic rules, and data transfer through integrations. Explain the proposed implementation and flag assumptions that require verification.

5. **Present the findings.** Display the recording alongside an interactive timeline and actionable recommendation cards, allowing users to inspect the evidence behind each finding.

A key technical distinction is that **understanding the business workflow does not necessarily require recovering every mouse click or keyboard shortcut**. Screenshots may reveal that information moved from an email into a spreadsheet without revealing exactly how it was transferred. The prototype should distinguish visible evidence from inferred actions.

The main challenges are selecting informative frames without losing important transitions, reading small interface text, preserving context across screenshots, recognizing repetitions with different data, and avoiding generic or unsupported recommendations. A production version would also need to handle interrupted tasks, activity across multiple applications, processing costs, and employee-controlled capture with appropriate privacy protections. Direct collection of application events could complement screenshots in a future version.

## Research directions

For the competitive and open-source research, we want to investigate several overlapping categories:

- Task mining and process discovery from desktop activity.
- Screen-recording analysis and automatic workflow documentation.
- Multimodal models that convert videos or screenshot sequences into structured action traces.
- Tools that discover automation opportunities from employee behavior.
- Systems that learn or generate automations from demonstrations.
- Enterprise products and startups offering similar observation-to-recommendation workflows.

The research should distinguish tools that document a workflow the user has already selected from tools that discover recurring workflows in broader activity. It should also distinguish describing a process, recommending an automation, and actually executing that automation.

For each relevant project or company, we want to understand its inputs, outputs, technical approach, availability, licensing, maturity, and whether any components could be reused for our hackathon prototype.
