// Starting points for the workflow builder. Each is an ordinary set of steps
// with built-in agents and tools; it is copied into the builder and nothing is
// saved until the person saves it.

export interface StepSeed {
  name: string
  agent_id: string
  purpose: string
  steps: string[]
  tools: string[]
  expected_output: string
  approval_required?: boolean
}

export interface WorkflowTemplate {
  id: string
  name: string
  /** the card line, and the saved workflow's description */
  summary: string
  description: string
  use_case: string
  trigger_examples: string[]
  phases: StepSeed[]
}

const TEMPLATES: Omit<WorkflowTemplate, 'description'>[] = [
  {
    id: 'phishing',
    name: 'Phishing email response',
    summary: 'Enrich the email, decide, pull it from mailboxes with approval, write it up.',
    use_case: 'A user reports a phishing email, or the mail gateway flags one.',
    trigger_examples: ['Run phishing response on this finding', 'Someone reported a suspicious email'],
    phases: [
      {
        name: 'Enrich the email',
        agent_id: 'threat_intel',
        purpose: 'Find out who sent it, what it links to and what it carries.',
        steps: ['Read the finding and pull sender, links and attachment hashes', 'Look up every link, domain and hash', 'Check whether anyone else received the same email'],
        tools: ['get_finding', 'lookup_indicators', 'search_findings', 'recall_entity'],
        expected_output: 'Sender, indicators with verdicts, and the list of recipients.',
      },
      {
        name: 'Decide malicious or benign',
        agent_id: 'investigator',
        purpose: 'Weigh the evidence and say what this is, with confidence.',
        steps: ['Check who clicked or opened the attachment', 'Compare with past phishing cases', 'Give a verdict and the reasons for it'],
        tools: ['get_finding', 'search_findings', 'recall_entity', 'get_case'],
        expected_output: 'Malicious, suspicious or benign, with the evidence for it.',
      },
      {
        name: 'Remove the email and contain',
        agent_id: 'responder',
        purpose: 'Pull the email from every mailbox and contain anyone who clicked.',
        steps: ['Propose deleting the email from all recipients', 'Propose a password reset for anyone who entered credentials', 'Block the sender domain'],
        tools: ['get_finding', 'update_case', 'create_approval_action', 'add_resolution_step'],
        expected_output: 'The containment actions proposed, each waiting for approval.',
        approval_required: true,
      },
      {
        name: 'Write it up',
        agent_id: 'reporter',
        purpose: 'Leave a record a manager can read in two minutes.',
        steps: ['Summarise what happened, who was affected and what was done', 'List the indicators'],
        tools: ['get_case', 'update_case', 'add_case_comment'],
        expected_output: 'A case summary with a timeline and indicators.',
      },
    ],
  },
  {
    id: 'identity',
    name: 'Suspicious sign-in',
    summary: 'Check the sign-in against the user’s history, link related alerts, revoke sessions with approval.',
    use_case: 'An impossible-travel, MFA-fatigue or unfamiliar-location sign-in alert.',
    trigger_examples: ['Investigate this sign-in', 'Was this sign-in really the user?'],
    phases: [
      {
        name: 'Check the sign-in',
        agent_id: 'triage',
        purpose: 'Decide whether the sign-in fits the user’s normal pattern.',
        steps: ['Read where, when and how the user signed in', 'Compare with their recent sign-ins and travel', 'Score how unusual it is'],
        tools: ['get_finding', 'search_findings', 'recall_entity'],
        expected_output: 'Normal or unusual, with what is different.',
      },
      {
        name: 'Link related activity',
        agent_id: 'correlator',
        purpose: 'Find anything else the account or source address did.',
        steps: ['Search for other alerts on the same user and address', 'Group them into one case if they belong together'],
        tools: ['search_findings', 'list_findings', 'create_case', 'add_finding_to_case'],
        expected_output: 'The related alerts and the case they were grouped into.',
      },
      {
        name: 'Revoke sessions',
        agent_id: 'responder',
        purpose: 'Cut off the attacker if the account is compromised.',
        steps: ['Propose revoking every session and resetting MFA'],
        tools: ['update_case', 'create_approval_action', 'add_resolution_step'],
        expected_output: 'The revocation, waiting for approval.',
        approval_required: true,
      },
    ],
  },
  {
    id: 'ransomware',
    name: 'Ransomware on an endpoint',
    summary: 'Scope the host, examine the files, isolate with approval, brief leadership.',
    use_case: 'An EDR alert for mass file encryption, shadow copy deletion or a known ransomware family.',
    trigger_examples: ['Ransomware on a finance workstation', 'Respond to this encryption alert'],
    phases: [
      {
        name: 'Scope the host',
        agent_id: 'investigator',
        purpose: 'Find out what ran, who ran it and what else it touched.',
        steps: ['Read the process tree and the user', 'Find other hosts it reached'],
        tools: ['get_finding', 'search_findings', 'recall_entity'],
        expected_output: 'The affected hosts and accounts.',
      },
      {
        name: 'Examine the files',
        agent_id: 'malware_analyst',
        purpose: 'Identify the family and its indicators.',
        steps: ['Look up the hashes', 'Pull out network indicators'],
        tools: ['get_finding', 'lookup_indicators'],
        expected_output: 'Family, indicators and what it does.',
      },
      {
        name: 'Isolate the host',
        agent_id: 'responder',
        purpose: 'Stop it spreading.',
        steps: ['Propose network isolation for every affected host'],
        tools: ['update_case', 'create_approval_action', 'add_resolution_step'],
        expected_output: 'Isolation proposed for each host, waiting for approval.',
        approval_required: true,
      },
      {
        name: 'Brief leadership',
        agent_id: 'reporter',
        purpose: 'Tell leadership what happened in plain words.',
        steps: ['Write a one-page brief: impact, actions, next steps'],
        tools: ['get_case', 'add_case_comment'],
        expected_output: 'An executive brief on the case.',
      },
    ],
  },
  {
    id: 'detection-gap',
    name: 'Detection gap review',
    summary: 'Map a case to ATT&CK, find what no rule caught, propose detections.',
    use_case: 'After an incident closes, or when a new threat report lands.',
    trigger_examples: ['What did we miss in this case?', 'Review detection coverage for this report'],
    phases: [
      {
        name: 'Map to ATT&CK',
        agent_id: 'mitre_analyst',
        purpose: 'Name every technique the case shows.',
        steps: ['Map each finding to a technique', 'Roll up by tactic'],
        tools: ['get_case', 'get_technique_rollup', 'analyze_coverage'],
        expected_output: 'Techniques seen, by tactic.',
      },
      {
        name: 'Find the gaps',
        agent_id: 'mitre_analyst',
        purpose: 'Say which techniques no rule covers.',
        steps: ['Search the detection library for each technique', 'List the ones with no rule'],
        tools: ['search_detections', 'identify_gaps', 'check_detection_candidate'],
        expected_output: 'Uncovered techniques and a draft rule for each.',
      },
      {
        name: 'Write it up',
        agent_id: 'reporter',
        purpose: 'Hand detection engineering a list they can act on.',
        steps: ['List each gap with its draft rule and the evidence'],
        tools: ['get_case', 'add_case_comment'],
        expected_output: 'A gap report on the case.',
      },
    ],
  },
]

export const WORKFLOW_TEMPLATES: WorkflowTemplate[] = TEMPLATES.map((t) => ({ ...t, description: t.summary }))
