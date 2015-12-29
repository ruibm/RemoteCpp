#include <iostream>

#include "SuperClass.h"
#include "SuperClass2.h"

int main() {
  SuperClass superClassInstance;
  SuperClass2 superClass2Instance;
  std::cout << "Very happilly running!! Weeeee... :)" << std::endl;
  function_defined_in_the_c_file();
  return 0;
}
