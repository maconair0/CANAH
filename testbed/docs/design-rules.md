# Design rules of the test bed network

## Optical domain
- A 100G lightpath needs a measured OSNR of at least 15.0 dB; a 400G lightpath at least 22.0 dB.
- A lightpath below its minimum OSNR must be re-routed or downgraded; packet services riding it are at risk.
- Each transponder carries one lightpath.

## Packet domain
- VLAN ids 100 to 999 are for customer services; 1000 and above are reserved for operations.
- A customer service between sites must ride a link carried by an optical lightpath.
- Interface errors above 5 on a hand-off port are to be reported as a fault.

## Data-centre fabric
- A tenant reaches outside the data centre only through border-leaf-1, so it must be attached to border-leaf-1 first.
- Tenant VNIs are in the range 5000 to 5999.
