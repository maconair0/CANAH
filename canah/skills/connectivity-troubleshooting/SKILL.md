---
name: connectivity-troubleshooting
description: Find out whether two hosts or sites really reach each other, and where the path breaks. Use for "can A reach B", "ping", "traceroute", "why can't X talk to Y", packet loss, or to prove a new service carries traffic.
---

# Connectivity troubleshooting

A controller that holds a service says it is configured. Whether traffic gets through is a different question,
and the only way to settle it is to send some: `net_ping`, `net_traceroute`, from hosts the network has
(`net_read_hosts`). They are reads: they send probes, they change nothing.

1. **Name the two ends.** Read the hosts (`net_read_hosts`): which host at each end, its address, its gateway, the
   router and port it hangs off. Use real names only; never ping an address you invented.
2. **Ping end to end** (`net_ping` src=<host>, dst=<the far host's address>). Answered: it works; say so with the
   numbers (sent, received, round-trip). Stop here unless asked for more.
3. **If it fails, go layer by layer**, because each layer fails for a different reason:
   - the host to its own gateway: fails → the host's access (its port, its router);
   - `net_traceroute` to the far host: the last hop that answered is where it stops, and the next one is broken.
4. **Map the broken hop to what carries it.** `follow_relations` from the IP service or link at that hop: the
   packet link, the lightpath under it, the fibre under that. Read each (`net_read_links`, `net_read_services`
   on the optical controller): the first one down or missing is the cause.
5. **Between sites over optical**, `ip_over_optical` gives the whole chain and the gap in one read.
6. **Report**: answered or not (with the numbers), the hop where it stops, the cause with the object and its
   state as read, and the fix as a change for approval, never as done. After a fix is approved and made,
   ping again: the ping is the proof, not the change's own answer.
