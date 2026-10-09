// The home page (§11.2): who we are and the two ways in, opening on Customer. Customers go to /support with
// no account; staff sign in on the Agent side. Public; only the sign-in form calls the API.

import { Welcome } from "@/components/welcome/welcome";

export default function Home() {
  return <Welcome initial="customer" />;
}
