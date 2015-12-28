#include "SuperClass.h"

#include <iostream>

SuperClass::SuperClass() {
  std::cout << "This is SuperClass being constructed!!!" << std::endl;
}

SuperClass::~SuperClass() {
  std::cout << "Bye bye from SuperClass!!!" << std::endl;
}
