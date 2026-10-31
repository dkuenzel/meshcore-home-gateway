// The companion's Advert button: its user interface calls MyMesh::advert()
// (examples/companion_radio/ui-new/UITask.cpp:588, ui-orig/UITask.cpp:557), which sends a
// zero-hop advert. The simulated board has no user interface, so the control line `advert`
// presses it (runtime.cpp).
#include "MyMesh.h"

extern MyMesh the_mesh;

void sim_button_advert() { the_mesh.advert(); }
