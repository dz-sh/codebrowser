#ifndef CODEBROWSER_SELFTEST_CONDITIONAL_HEADER_H
#define CODEBROWSER_SELFTEST_CONDITIONAL_HEADER_H

#if 0
static inline int selftest_inactive_header_branch(void)
{
    return 0;
}
#else
static inline int selftest_active_header_branch(void)
{
    return 1;
}
#endif

#endif
