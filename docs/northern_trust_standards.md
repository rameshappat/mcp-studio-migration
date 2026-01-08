# Northern Trust Technical Standards and Requirements

## Technology Stack Preferences

### Cloud Platform
- **Azure** - All infrastructure must be Azure-native

### Frontend
- **React JS** - Primary frontend framework

### Backend  
- **Java-based Microservices** - Spring Boot recommended

## Secure Software Development Life Cycle (SSDLC) Requirements

### 2026 Banking & Financial Secure Coding Standards

In 2026, banking and financial secure coding standards are primarily driven by updated regulations focused on risk-based fraud monitoring, the need for secure software development life cycles (SSDLC), and adherence to established cybersecurity frameworks like PCI DSS and NIST.

#### Key Regulatory Drivers and Standards in 2026

The industry does not follow a single, universal "2026 standard" but rather aligns with updates to existing, mandatory regulations and best practices.

##### Nacha Operating Rules Amendments

The most significant changes effective in 2026 are Nacha's new rules for Automated Clearing House (ACH) participants.

- **Mandate**: All participants must establish and implement risk-based processes to identify and monitor ACH entries suspected of being fraudulent (specifically targeting credit-push fraud and business email compromise).
- **Phased Implementation**: 
  - Phase 1 begins March 20, 2026, for larger institutions
  - Phase 2 follows on June 19, 2026, covering all remaining participants
- **Secure Coding Impact**: Systems must be developed with data-driven, continuous monitoring capabilities, moving beyond static, threshold-based rules to analyzing behavioral patterns and contextual data.
- **Standardized Descriptions**: Originators of certain transactions must use standardized "Company Entry Description" fields (e.g., "PAYROLL" for payroll credits) to aid automated fraud detection, which requires specific coding changes.

##### CFPB Open Banking Rule (Section 1033)

Compliance begins in April 2026 for the largest entities, requiring financial institutions to share consumer financial data with consumers and authorized third parties.

- **Secure Coding Impact**: This mandates the use of secure, well-documented APIs (Application Programming Interfaces), likely using standards like OAuth 2.0 and OpenID Connect, to ensure data is shared securely without compromising sensitive systems.

##### Digital Operational Resilience Act (DORA) (EU)

Although an EU regulation, DORA impacts global financial entities operating there. It emphasizes strict Information and Communication Technology (ICT) risk management, regular testing, and third-party oversight.

- **Secure Coding Impact**: Promotes a centralized ICT risk framework and integration of security into the entire development lifecycle (DevSecOps).

#### Fundamental Secure Coding Practices for 2026

Beyond specific regulations, financial institutions must embed security into their software development processes, utilizing established frameworks:

##### Secure-by-Design and DevSecOps
Security can no longer be an afterthought. Secure coding practices, automated security testing (vulnerability scanning, dependency analysis) within CI/CD pipelines, and regular penetration testing must be integral parts of the development lifecycle.

##### Data Protection & Encryption
End-to-end encryption for data in transit and at rest, secure key management systems, and tokenization of sensitive data are baseline requirements.

##### Strong Identity and Access Management (IAM)
Secure coding standards demand robust authentication mechanisms, including:
- Multi-Factor Authentication (MFA)
- Biometric authentication
- Role-Based Access Control (RBAC)

##### Compliance with Industry Frameworks

Financial institutions adhere to various frameworks to guide their secure coding efforts, including:

- **Payment Card Industry Data Security Standard (PCI DSS)**: Mandatory for any entity handling payment card data, focusing on secure application development and cardholder data protection.
- **NIST Cybersecurity Framework**: Provides a flexible, risk-based approach (Identify, Protect, Detect, Respond, Recover) that is widely used in the US financial sector.
- **ISO 27001/27002**: Widely adopted global standards for information security management systems.

## References

- [Nacha Operating Rules](https://www.nacha.org/newrules)
- [C9Lab Banking Cybersecurity 2026](https://c9lab.com/blog/banking-cybersecurity-2026-emerging-threats-defense-strategies/)
- [CFPB Open Banking Rule](https://www.moodys.com/web/en/us/insights/regulatory-news/cfpb-finalizes-open-banking-rule--compliance-begins-april-2026.html)
- [Secure Development for Fintech Apps](https://www.diginnovators.com/blog/secure-development-for-fintech-apps/)
- [StrongDM Cybersecurity Regulations](https://www.strongdm.com/blog/cybersecurity-regulations-financial-industry)
