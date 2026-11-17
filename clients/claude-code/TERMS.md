# Terms of Use

**Effective date:** 2026-11-16
**Applies to:** the Vigil software, the Vigil SOC plugin for Claude Code, and the website vigilsoc.org.


## 1. What these terms are

Vigil is open source software published by the Vigil project, maintained by DeepTempo.AI and project supporters ("we", "us"). The software and the Vigil SOC plugin for Claude Code (together, "the Software") are licensed under the Apache License, Version 2.0 (<https://www.apache.org/licenses/LICENSE-2.0>). The license governs your rights to use, copy, modify, and distribute the Software. These terms add only what the license does not say: how the plugin relates to your own systems, what you are responsible for, and how to reach us. If these terms and the Apache License conflict, the license controls for the Software's source code.

By installing or using the Software or using vigilsoc.org, you agree to these terms.

## 2. No hosted service

We do not operate Vigil as a service. The Software runs on infrastructure you control. The plugin connects your Claude Code session to a Vigil instance you or your organization operate, at an address and with a credential you supply. We do not host, monitor, or have access to that instance, and we provide no uptime, support, or availability commitment for it.

## 3. Your responsibilities

You are responsible for:

- **Your instance.** Deploying, securing, updating, and operating your Vigil instance, including network exposure, TLS, access control, backups, and retention. The project's security guidance is in [SECURITY.md](https://github.com/Vigil-SOC/vigil/blob/main/SECURITY.md) and the deployment documentation.
- **Your credentials.** Keeping the credentials you mint for the MCP surface confidential, minting them only for people entitled to the role they carry, and revoking them when no longer needed. Actions taken with a credential are recorded against its holder.
- **Authorized use.** Using the Software only on systems, networks, and data that you own or are authorized to monitor and investigate. Vigil is a security operations tool. You must comply with all laws that apply to your use of it, including computer misuse, privacy, and data protection laws.
- **Your model provider.** Data your Claude Code session sends to a model provider is governed by that provider's terms. Choosing the provider, and the plan and data-handling settings under it, is your decision.
- **Human review.** Vigil is designed so that a person approves consequential actions. You are responsible for the actions your people approve and for how you configure the autonomy ceiling, roles, and approval rules in your instance.
- **Third-party services.** Any integration you enable in Vigil is subject to that service's terms.

## 4. Acceptable use

You may not use the Software or the plugin to:

- access, monitor, or act on systems or data without authorization;
- circumvent the safety, permission, or approval controls of Vigil, of Claude Code, or of any model provider;
- violate the acceptable use policy of a model provider you route the Software through;
- misrepresent the Software as an official product of Anthropic or of any other company.

## 5. Open source, contributions, and trademarks

The Software's source code is available at <https://github.com/Vigil-SOC/vigil>. Contributions are accepted under the Developer Certificate of Origin described in the repository and are licensed under the Apache License 2.0. "Vigil" and the Vigil mark are used to identify this project; the Apache License does not grant trademark rights, and you may not use the name or mark in a way that suggests we endorse a modified version or a service you operate.

## 6. Disclaimer of warranty

The Software and the website are provided "AS IS", without warranty of any kind, as set out in Section 7 of the Apache License 2.0. Without limiting that, we do not warrant that the Software will detect any particular threat, that its findings, investigations, or hunts are complete or correct, or that it is suitable for any regulatory or compliance purpose. Output produced with the help of a language model can be wrong and must be reviewed by a qualified person before it is relied on.

## 7. Limitation of liability

To the extent permitted by law, and as set out in Section 8 of the Apache License 2.0, we are not liable for any direct, indirect, incidental, special, or consequential damages arising from your use of the Software, the plugin, or the website, including loss of data, business interruption, or any action taken or not taken on the basis of the Software's output. Some jurisdictions do not allow these limitations, in which case they apply to the fullest extent permitted.

## 8. Changes

We may change the Software and these terms. The current terms are at <https://vigilsoc.org/terms>, with their history in the project repository. Continued use after a change means you accept the changed terms. The Apache License 2.0 that applies to a version of the Software you already received does not change.

## 9. Governing law

These terms are governed by the laws of California, USA, without regard to its conflict-of-law rules.

## 10. Contact

- Questions about these terms: security@deeptempo.ai
- Security reports: <https://vigilsoc.org/.well-known/security.txt>
- Issues and support: <https://github.com/Vigil-SOC/vigil/issues>
