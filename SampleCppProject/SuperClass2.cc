#include "SuperClass2.h"

#include <iostream>

SuperClass2::SuperClass2() {
  std::cout << "Constructor of a completely new flavour of SuperClass2."
      << std::endl;
}

SuperClass2::~SuperClass2() {
  std::cout << "SuperClass2 fading away..... :(" << std::endl;
}
