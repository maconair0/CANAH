# The test bed's network

Three domains, each with its own controller, reached through the agentic MCP:

- **packet** (PacketCtl): routers at site-a and site-b, the links between them, VLAN services.
- **optical** (OptiCtl): ROADMs and transponders at site-a, site-b and site-c, and the lightpaths between transponders.
- **fabric** (FabricCtl): the data centre at site-b, a leaf-spine fabric with tenants.

The domains meet at hand-offs each side declares: a packet port facing an optical transponder's client port, or the
fabric's border leaf. A packet link can be carried by an optical lightpath. A fabric tenant leaves the data centre only
through the border leaf.
