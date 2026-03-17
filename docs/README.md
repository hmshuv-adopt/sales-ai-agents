# Your Knowledge Base

Place your documents in this folder for the RAG system to index.

## Supported File Types
- Markdown files (`.md`)
- Text files (`.txt`)
- PDF files (`.pdf`)

## Recommended Document Structure

Organize your documents by category:

```
docs/
├── capabilities/
│   ├── zapi.md
│   ├── agent-builder.md
│   └── deployment-options.md
├── use-cases/
│   ├── customer-support.md
│   ├── sales-automation.md
│   └── internal-tools.md
├── value-props/
│   ├── enterprise-security.md
│   └── time-to-value.md
├── personas/
│   ├── cto-messaging.md
│   ├── vp-engineering.md
│   └── product-manager.md
├── case-studies/
│   ├── acme-inc.md
│   └── spacex-inc.md
└── industries/
    ├── fintech.md
    └── healthcare.md

```

## Indexing

After adding documents, run:

```bash
python index_knowledge_base.py
```

This will:
1. Load all documents from this folder
2. Split them into chunks
3. Generate embeddings using sentence-transformers
4. Store in ChromaDB (persisted in `.chroma/` folder)

## Usage in Lead Finder

The lead finder agent will automatically search this knowledge base for:
- Relevant capabilities based on the lead's industry
- Use cases matching their company profile
- Value propositions for their persona (job title/seniority)
- Similar case studies

This context is then included in the AI prompt to generate more personalized outreach emails.
