import type { NextConfig } from "next";

/**
 * Where /api/pay/* is proxied to: the backend from NEXT_PUBLIC_API_URL. This server fetches it, not
 * the browser, so "localhost" becomes 127.0.0.1 (ARCHITECTURE.md section 4.5: on an IPv4-only
 * network, localhost costs 250 ms per connection before falling back to IPv4).
 */
function backendOrigin(): string {
  const value = process.env.NEXT_PUBLIC_API_URL;
  if (!value) {
    throw new Error("NEXT_PUBLIC_API_URL is not set. Copy web/.env.local.example to web/.env.local.");
  }
  const url = new URL(value);
  if (url.hostname === "localhost") url.hostname = "127.0.0.1";
  return url.origin;
}

const nextConfig: NextConfig = {
  // A phone opens the pay page through a Cloudflare quick tunnel to this server (README). The dev
  // server refuses dev assets to any hostname but its own unless it is listed here.
  allowedDevOrigins: ["*.trycloudflare.com"],
  // The floating dev badge sits over the pay page's QR code on a phone and can spoil a scan.
  // Compile and runtime errors are still shown without it.
  devIndicators: false,

  // The pay page calls /api/pay/* on this origin, and this server proxies it to the backend, so one
  // tunnel to port 3000 serves a phone both the page and its API (section 7.6).
  async rewrites() {
    return [{ source: "/api/pay/:path*", destination: `${backendOrigin()}/api/pay/:path*` }];
  },
};

export default nextConfig;