# Privacy Policy

**Effective date:** 2026-11-15
**Applies to:** the Vigil software, the Vigil SOC plugin for Claude Code, and the website vigilsoc.org.


## The short version

Vigil is open source software that you run on your own infrastructure. The Vigil SOC plugin for Claude Code connects your Claude Code session to your own Vigil instance. The Vigil project operates no hosted service, receives no data from your Vigil instance or from the plugin, and collects no analytics or telemetry from either. The only data the project handles is what you send us directly, such as an email or a GitHub issue.

## 1. Who we are

Vigil is an open source project published under the Apache License 2.0 at <https://github.com/Vigil-SOC/vigil> and maintained by DeepTempo.AI and project supporters. In this policy, "we", "us" and "the project" mean the Vigil project and its maintainers. "You" means the person or organization installing or using the software, the plugin, or the website.

## 2. What this policy covers

This policy describes what data each part of the project handles, where that data goes, and what we do and do not receive. It covers:

- the Vigil software you install and run (section 3);
- the Vigil SOC plugin for Claude Code (section 4);
- the website vigilsoc.org (section 5).

It does not cover the services you choose to connect to Vigil, such as a model provider, a security tool, or a threat-intelligence feed. Each of those is governed by its own provider's policy.

## 3. The Vigil software

**Where it runs.** Vigil runs where you install it: on your own machine, in your own containers, or in your own cluster. All data it processes, including security findings, cases, indicators, comments, approvals, audit entries, and the credentials of the people who use it, is stored in the database you operate. We have no access to it.

**What it sends to us.** Nothing. The software does not contact the Vigil project, DeepTempo, or vigilsoc.org during installation or operation. It performs no update checks, sends no crash reports to us, and carries no analytics.

**Outbound connections you configure.** Vigil makes outbound connections only to services you configure, and each is off until you turn it on. These include:

- the large language model provider or providers you configure, which receive the prompts and context Vigil's agents send them;
- integrations you enable in Settings, such as detection, ticketing, or threat-intelligence services;
- an OpenTelemetry collector, only when you set an exporter endpoint;
- an error-reporting service, only when you set a Sentry DSN;
- the Vigil MCP surface at `/mcp`, which is closed until an operator opens it and which accepts only credentials you mint.

Data sent to those services is sent on your behalf to the party you chose, under that party's terms and privacy policy.

**Audit trail.** Your Vigil instance records who did what on its audit trail, including calls made through the MCP surface. That record is stored in your database and retained according to your own configuration and policies.

**Your role.** For personal data processed by your Vigil instance, you or your organization are the data controller. The Vigil project is neither a controller nor a processor of that data, because it never receives it.

## 4. The Vigil SOC plugin for Claude Code

**What it is.** The plugin is a set of skill files and one MCP server entry. It contains no executable code of its own and runs no server. When installed, Claude Code asks you for two values: the address of your Vigil instance and a credential you minted in that instance.

**What data it handles.** When you use a plugin skill, Claude Code sends requests to the `/mcp` surface of the Vigil instance at the address you entered, authenticated with your credential, and receives results such as findings, cases, indicators, comments, run status, approval actions, and audit entries. Those results may contain personal data held in your security data, such as user names, email addresses, IP addresses, and host names. That data belongs to your organization and is governed by section 3.

**Where the data goes.** Data flows in exactly two directions:

1. between Claude Code and the Vigil instance you configured;
2. into your Claude Code session, and from there to whichever model provider your Claude Code is configured to use. For most installations that is Anthropic, under Anthropic's own privacy policy and the terms of your Claude plan. If you route Claude Code to a model inside your own environment, the data stays inside it.

The plugin sends nothing to the Vigil project, to DeepTempo, or to vigilsoc.org. It contains no analytics, telemetry, or tracking.

**What it stores.** The plugin itself stores nothing. Claude Code stores the Vigil address in its plugin settings and stores the credential in the platform's secure storage, because the plugin marks it as sensitive. Claude Code may also keep session transcripts on your machine according to its own settings. No file in the plugin holds an address or a secret.

**Retention.** The plugin retains no data. Your Vigil instance retains the audit record of each call under your own retention settings. Retention of Claude Code sessions and of data sent to your model provider is governed by Anthropic's policies or by the policies of the provider you chose.

**Revoking access.** An operator of your Vigil instance can close the MCP surface or revoke a credential at any time, which stops the plugin from reaching that instance.

## 5. The website vigilsoc.org

vigilsoc.org is a documentation and community site hosted on GitHub Pages. It has no user accounts, no forms that collect personal data, and no analytics or tracking cookies. GitHub, as the hosting provider, may keep standard server logs, including IP addresses and user agents, for security and operational purposes under GitHub's own privacy statement. Pages load fonts from Google Fonts and the home page loads GitHub's button script, so those providers receive the same request data when a page is opened.

## 6. Data we do receive

We receive personal data only when you send it to us directly, for example by opening a GitHub issue or discussion, submitting a pull request, or emailing us. GitHub processes that data under GitHub's privacy statement. We use email you send us only to respond to you and to maintain the project.

## 7. Children

Vigil, the plugin, and the website are intended for security professionals and are not directed at people under 18. We do not knowingly collect personal data from anyone under 18.

## 8. Security

We take reports of security vulnerabilities through GitHub private vulnerability reporting or the address published in <https://vigilsoc.org/.well-known/security.txt>. See [SECURITY.md](https://github.com/Vigil-SOC/vigil/blob/main/SECURITY.md) for the process and supported versions.

## 9. Your rights and choices

Because the project holds no data from your Vigil instance or the plugin, requests about that data should go to the operator of the Vigil instance you use. For data you sent to us directly, you may ask us to correct or delete it by contacting us at the address below. Depending on where you live, you may have additional rights under applicable data protection law, and we will honor them.

## 10. Changes to this policy

We may update this policy as the software changes. The current version is always at <https://vigilsoc.org/privacy>, and its history is in the project repository. Material changes will be noted in the project changelog.

## 11. Contact

- Privacy questions: security@deeptempo.ai
- Security reports: see <https://vigilsoc.org/.well-known/security.txt>
- General: <https://github.com/Vigil-SOC/vigil/issues>
