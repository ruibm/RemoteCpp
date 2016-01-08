#include <iostream>

#include "SuperClass.h"
#include "SuperClass2.h"
#include "more_code/SuperClass3.h"

int main() {
  SuperClass superClassInstance;
  SuperClass2 superClass2Instance;
  SuperClass3 superClass3Instance;
  std::cout << "Very happilly running!! Weeeee... :)" << std::endl;
  function_defined_in_the_c_file();
  return 0;
}
