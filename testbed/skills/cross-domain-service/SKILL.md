---
name: cross-domain-service
description: How to answer about, or build, something that spans more than one network domain (packet over optical, a data-centre tenant reaching another site). Use when a request names objects from two domains, or asks what a fault in one domain affects in another.
---

# Across domains

1. Find the hand-offs first: read each domain's ports and links (`net_read_ports`, `net_read_links`, with no controller to read them all) to see which of its objects face another domain, and what
   they face (a packet port facing a transponder's client port, a border leaf's uplink).
2. Follow the chain end to end, one domain at a time, through those hand-offs. Name each piece with the domain it is in.
3. For an impact question (a fault in one domain): start from the faulty object, follow the hand-offs outward, and list
   what rides on it in each domain (links, services, tenants, customers).
4. To build: create the lower layer first (the lightpath before the packet service it carries; the packet service
   before a tenant uses it). Each change pauses for approval. Verify each piece with a read before the next.
5. Report the chain as a list, each piece with its domain and its state.
