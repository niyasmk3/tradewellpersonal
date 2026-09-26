/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // NEXT_DIST_DIR lets a second dev/build instance (e.g. a verification
  // preview on another port) run without corrupting the primary server's
  // .next directory. Unset = the default, no behaviour change.
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

export default nextConfig;
