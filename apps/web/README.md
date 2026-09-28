# AllerNav Web

The Next.js, React, and TypeScript frontend provides restaurant discovery, an interactive map, allergy selection, menu evidence, nearby suggestions, and community reviews. Next.js API routes serve web features and bridge to the FastAPI agent backend.

Start with the root [project overview](../../README.md) and [setup guide](../../docs/setup.md). Copy [.env.example](.env.example) to `.env.local`, configure the required settings, and run `npm run dev` from the repository root.

For Vercel, use `apps/web` as the web project's root directory. FastAPI and the optional Azure Functions worker are separate deployment targets.
