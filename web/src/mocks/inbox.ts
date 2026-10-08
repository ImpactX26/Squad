/**
 * Mock inbox data, used until /inbox runs on the real GET /api/tickets (§14.6: mocks shaped
 * exactly like the Pydantic schema). The tickets are typed as Schemas["TicketList"], so a
 * schema change breaks this file at compile time.
 *
 * The conversations are provisional: there is no timeline endpoint yet (GET
 * /api/tickets/{id}/timeline is Block 2), so their shape follows the messages table (§8.1).
 */

import type { Schemas } from "@/lib/api";

type TicketRow = Schemas["TicketRow"];

export type MockMessage = {
  id: string;
  sender_type: "customer" | "ai" | "agent";
  author: string | null;
  channel: TicketRow["source_channel"];
  body: string;
  created_at: string;
};

const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();
const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;

const RIYA = { id: id(101), name: "Riya Sharma" };
const AMAN = { id: id(102), name: "Aman Verma" };
const SNEHA = { id: id(103), name: "Sneha Kulkarni" };
const RAHUL = { id: id(104), name: "Rahul Nair" };

/** The inbox as GET /api/tickets returns it; `me` is the signed-in agent's id. */
export function mockTickets(me: string): TicketRow[] {
  const row = (r: Partial<TicketRow> & Pick<TicketRow, "id" | "ticket_number" | "title" | "customer">): TicketRow => ({
    status: "new",
    priority: "medium",
    category: "hardware",
    issue_type: "other",
    source_channel: "web",
    channels: [r.source_channel ?? "web"],
    device: null,
    ai_summary: null,
    duplicate_count: 0,
    flags: [],
    assigned_agent_id: null,
    created_at: minutesAgo(60),
    updated_at: minutesAgo(60),
    ...r,
  });

  return [
    row({
      id: id(1), ticket_number: "SR-2026-00042", title: "Battery not charging", customer: RIYA,
      status: "awaiting_customer", priority: "high", issue_type: "battery", source_channel: "discord",
      channels: ["discord", "email"], duplicate_count: 2, assigned_agent_id: me,
      device: { model_name: "Aurora 14", serial_number: "AX14-7F3K92", color: "Silver", category: "laptop" },
      ai_summary: "Aurora 14 won't charge past 0% even with another adapter. Out of warranty since June. Riya followed up by email asking for an update; likely battery replacement.",
      created_at: minutesAgo(190), updated_at: minutesAgo(2),
    }),
    row({
      id: id(2), ticket_number: "SR-2026-00043", title: "Stuck at 20% charge", customer: AMAN,
      status: "new", priority: "medium", issue_type: "charging", source_channel: "telegram",
      device: { model_name: "Vertex 15", serial_number: "VX15-Q8M2D5", color: "Shadow Black", category: "laptop" },
      ai_summary: "Vertex 15 stops charging at 20% since yesterday; the charger LED is on. In warranty until March 2027. Plan: check the adapter, then battery health in BIOS.",
      created_at: minutesAgo(6), updated_at: minutesAgo(6),
    }),
    row({
      id: id(3), ticket_number: "SR-2026-00044", title: "Left earcup has no sound", customer: SNEHA,
      status: "in_progress", priority: "low", issue_type: "audio", source_channel: "email", assigned_agent_id: me,
      device: { model_name: "Pulse Air 7", serial_number: "PA7-3KX9TB", color: "Midnight Blue", category: "headphones" },
      ai_summary: "Pulse Air 7 plays on the right side only, wired and over Bluetooth. In warranty. Firmware update suggested before an exchange.",
      created_at: minutesAgo(300), updated_at: minutesAgo(45),
    }),
    row({
      id: id(4), ticket_number: "SR-2026-00045", title: "Screen flickers when the lid opens wide", customer: RAHUL,
      status: "new", priority: "medium", issue_type: "display", source_channel: "web",
      device: { model_name: "Lumen Book 13", serial_number: "LB13-W4N7PC", color: "Mist Grey", category: "laptop" },
      ai_summary: "Lumen Book 13 flickers past about 110° of hinge angle, fine when nearly closed. Points to the display cable. In warranty.",
      created_at: minutesAgo(14), updated_at: minutesAgo(11),
    }),
    row({
      id: id(5), ticket_number: "SR-2026-00041", title: "Won't boot after update", customer: { id: id(105), name: "Karthik Rao" },
      status: "in_progress", priority: "urgent", category: "software", issue_type: "boot", source_channel: "telegram",
      channels: ["telegram", "web"], duplicate_count: 1,
      device: { model_name: "Aurora 16 Pro", serial_number: "AX16-M2P8QK", color: "Graphite", category: "laptop" },
      ai_summary: "Aurora 16 Pro loops to the logo after last night's BIOS update. Customer needs it for work tomorrow. Recovery steps sent; escalate if they fail.",
      created_at: minutesAgo(400), updated_at: minutesAgo(25),
    }),
    row({
      id: id(6), ticket_number: "SR-2026-00040", title: "Keyboard keys repeat", customer: { id: id(106), name: "Meera Iyer" },
      status: "awaiting_payment", priority: "medium", issue_type: "keyboard", source_channel: "discord", assigned_agent_id: me,
      device: { model_name: "Vertex 14", serial_number: "VX14-T7B3NE", color: "Silver", category: "laptop" },
      ai_summary: "Several keys on the Vertex 14 type twice. Out of warranty; keyboard replacement quoted and the payment link sent.",
      created_at: minutesAgo(1500), updated_at: minutesAgo(70),
    }),
    row({
      id: id(7), ticket_number: "SR-2026-00046", title: "Desktop shuts down under load", customer: { id: id(107), name: "Imran Sheikh" },
      status: "new", priority: "high", issue_type: "overheating", source_channel: "email",
      device: { model_name: "Aurora Tower 7", serial_number: "AT7-9HX2LM", color: null, category: "desktop" },
      ai_summary: "Aurora Tower 7 powers off a few minutes into games. Fans loud before it happens. Likely thermal; dust or a failed fan.",
      created_at: minutesAgo(38), updated_at: minutesAgo(38),
    }),
    row({
      id: id(8), ticket_number: "SR-2026-00047", title: "Wi-Fi drops every few minutes", customer: { id: id(108), name: "Neha Gupta" },
      status: "new", priority: "low", category: "unknown", issue_type: "connectivity", source_channel: "web",
      flags: ["unverified_product"],
      ai_summary: "Laptop loses Wi-Fi every few minutes on any network. The serial given didn't match our records; asked again.",
      created_at: minutesAgo(52), updated_at: minutesAgo(50),
    }),
    row({
      id: id(9), ticket_number: "SR-2026-00039", title: "Headphones won't pair", customer: { id: id(109), name: "Vikram Joshi" },
      status: "resolved", priority: "low", issue_type: "connectivity", source_channel: "telegram",
      device: { model_name: "Pulse Air 5", serial_number: "PA5-2RX8VD", color: "White", category: "headphones" },
      ai_summary: "Pulse Air 5 wouldn't pair with a new phone. Fixed by the factory reset in step 2.",
      created_at: minutesAgo(2900), updated_at: minutesAgo(1300),
    }),
    row({
      id: id(10), ticket_number: "SR-2026-00048", title: "Charger smells of burning", customer: { id: id(110), name: "Ananya Das" },
      status: "new", priority: "urgent", issue_type: "charging", source_channel: "discord", flags: ["ownership_mismatch"],
      device: { model_name: "Aurora 13", serial_number: "AX13-5KQ7RW", color: "Rose", category: "laptop" },
      ai_summary: "Charger of an Aurora 13 smells of burning plastic. Told to unplug it at once. The serial is registered to another customer: check before acting.",
      created_at: minutesAgo(3), updated_at: minutesAgo(3),
    }),
  ];
}

/** What each customer first wrote, in their own words. */
const OPENING: Record<string, string> = {
  [id(1)]: "Hi, my Aurora 14 won't charge past 0%. I tried another adapter, same thing. Serial AX14-7F3K92.",
  [id(2)]: "My Vertex 15 stopped charging at 20% since yesterday. Charger light is on. Serial VX15-Q8M2D5",
  [id(3)]: "Hello, my Pulse Air 7 only plays on the right side, wired and Bluetooth. Serial PA7-3KX9TB.",
  [id(4)]: "The screen on my Lumen Book 13 flickers when I open the lid all the way. LB13-W4N7PC",
  [id(5)]: "Laptop is stuck on the logo since last night's BIOS update. I need it for work tomorrow!",
  [id(6)]: "A few keys on my Vertex 14 type twice. Serial VX14-T7B3NE.",
  [id(7)]: "My Aurora Tower 7 shuts off a few minutes into any game. The fans get really loud first.",
  [id(8)]: "Wi-Fi keeps dropping every few minutes on my laptop, on every network.",
  [id(9)]: "My Pulse Air 5 won't pair with my new phone.",
  [id(10)]: "My charger smells like burning plastic!! Aurora 13, serial AX13-5KQ7RW",
};

/** Provisional conversations per ticket (see the note at the top). */
export function mockConversation(ticket: TicketRow): MockMessage[] {
  const at = Date.parse(ticket.created_at);
  const step = Math.max(60_000, (Date.parse(ticket.updated_at) - at) / 4);
  const time = (i: number) => new Date(at + i * step).toISOString();
  const first = ticket.customer.name?.split(" ")[0] ?? "there";
  const device = ticket.device ? `${ticket.device.model_name} (serial ${ticket.device.serial_number})` : "your device";
  const second = ticket.channels[1] ?? ticket.source_channel;

  const messages: MockMessage[] = [
    { id: `${ticket.id}-1`, sender_type: "customer", author: ticket.customer.name, channel: ticket.source_channel,
      body: OPENING[ticket.id] ?? ticket.title, created_at: time(0) },
    { id: `${ticket.id}-2`, sender_type: "ai", author: null, channel: ticket.source_channel,
      body: ticket.device
        ? `Thanks ${first}. I've opened ticket ${ticket.ticket_number} for your ${device}. An agent will pick it up shortly.`
        : `Thanks ${first}. Could you send the serial number? It's on the sticker under the laptop, or run "wmic bios get serialnumber" on Windows.`,
      created_at: time(1) },
  ];
  if (ticket.duplicate_count > 0) {
    messages.push({ id: `${ticket.id}-3`, sender_type: "customer", author: ticket.customer.name, channel: second,
      body: "Any update on this? It's still happening.", created_at: time(2) });
  }
  if (ticket.assigned_agent_id) {
    messages.push({ id: `${ticket.id}-4`, sender_type: "agent", author: "Arjun Mehta", channel: second,
      body: `Hi ${first}, I'm looking into it now and will update you within the hour.`, created_at: time(3) });
  }
  return messages;
}
