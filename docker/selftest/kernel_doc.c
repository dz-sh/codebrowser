#include "conditional_header.h"

#define SELFTEST_FEATURE 1

#if SELFTEST_FEATURE
int selftest_active_branch(void)
{
    return selftest_active_header_branch(); /* SELFTEST_ACTIVE_SENTINEL */
}
#else
int selftest_inactive_branch(void)
{
    return -1; /* SELFTEST_INACTIVE_SENTINEL */
}
#endif

/**
 * release_pages() - release pages
 * @pfn: start PFN to free
 */
void release_pages(unsigned long pfn)
{
    (void)pfn;
}
