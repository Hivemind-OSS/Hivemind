#include <stdio.h>
#include "util.h"

int compute(int x) {
    return square(x) + 1;
}

int main(void) {
    int r = compute(3);
    printf("%d\n", r);
    return 0;
}
