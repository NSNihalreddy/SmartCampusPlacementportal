document.addEventListener("DOMContentLoaded", () => {
    const body = document.body;
    const mobileQuery = window.matchMedia("(max-width: 850px)");

    function setupSidebar(role) {
        const toggle = document.querySelector(
            `[data-sidebar-toggle="${role}"]`
        );

        if (!toggle) return;

        const isFaculty = role === "faculty";
        const collapsedClass = isFaculty
            ? "faculty-sidebar-collapsed"
            : "student-sidebar-collapsed";

        const expandedMobileClass = isFaculty
            ? "faculty-sidebar-expanded-mobile"
            : "student-sidebar-expanded-mobile";

        const storageKey = isFaculty
            ? "facultySidebarCollapsed"
            : "studentSidebarCollapsed";

        // Restore desktop preference only.
        try {
            if (
                !mobileQuery.matches &&
                localStorage.getItem(storageKey) === "1"
            ) {
                body.classList.add(collapsedClass);
            }
        } catch (_) {}

        function closeMobileSidebar() {
            body.classList.remove(expandedMobileClass);
            toggle.setAttribute("aria-expanded", "false");
            backdrop.hidden = true;
        }

        // Create a backdrop without changing any HTML templates.
        const backdrop = document.createElement("div");
        backdrop.className = "mobile-sidebar-backdrop";
        backdrop.hidden = true;
        backdrop.addEventListener("click", closeMobileSidebar);
        document.body.appendChild(backdrop);

        toggle.addEventListener("click", () => {
            if (mobileQuery.matches) {
                const isOpen = body.classList.toggle(expandedMobileClass);

                toggle.setAttribute(
                    "aria-expanded",
                    isOpen ? "true" : "false"
                );

                backdrop.hidden = !isOpen;
                return;
            }

            body.classList.toggle(collapsedClass);

            const collapsed = body.classList.contains(collapsedClass);

            toggle.setAttribute(
                "aria-expanded",
                collapsed ? "false" : "true"
            );

            toggle.title = collapsed
                ? "Expand sidebar"
                : "Collapse sidebar";

            try {
                localStorage.setItem(
                    storageKey,
                    collapsed ? "1" : "0"
                );
            } catch (_) {}
        });

        // Close mobile drawer after selecting a navigation destination.
        const sidebar = document.querySelector(
            isFaculty ? ".faculty-sidebar" : ".student-sidebar"
        );

        if (sidebar) {
            sidebar.querySelectorAll("a").forEach(link => {
                link.addEventListener("click", () => {
                    if (mobileQuery.matches) {
                        closeMobileSidebar();
                    }
                });
            });
        }

        // Reset mobile-only state when moving back to desktop.
        mobileQuery.addEventListener("change", event => {
            if (!event.matches) {
                closeMobileSidebar();
            } else {
                body.classList.remove(collapsedClass);
            }
        });
    }

    setupSidebar("student");
    setupSidebar("faculty");

    // Existing faculty navigation groups: preserve dropdown functionality.
    document.querySelectorAll(".sidebar-group-toggle").forEach(button => {
        button.addEventListener("click", () => {
            const group = button.closest(".sidebar-group");
            if (!group) return;

            group.classList.toggle("is-open");

            button.setAttribute(
                "aria-expanded",
                group.classList.contains("is-open") ? "true" : "false"
            );
        });
    });
});