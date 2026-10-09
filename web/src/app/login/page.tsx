// Staff sign-in (§11.2): the welcome screen opening on Agent. Agents go to /inbox, technicians to /jobs,
// warehouse staff to /inventory. Customers never need an account: the Customer side sends them to
// /support. Escape goes back to the home page.

import { EscapeHome } from "@/components/escape-home";
import { Welcome } from "@/components/welcome/welcome";

export default function LoginPage() {
  return (
    <>
      <EscapeHome />
      <Welcome initial="agent" />
    </>
  );
}
