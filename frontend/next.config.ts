import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // This app lives inside the Python agent repo, and there are lockfiles above
  // it (a package-lock.json in the home directory, pnpm-lock.yaml next door).
  // Pin the workspace root so Turbopack does not watch the whole home folder.
  turbopack: {
    root: __dirname,
  },
};

export default nextConfig;
