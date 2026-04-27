import java.lang.instrument.*;
import java.io.*;
import java.awt.*;
import java.awt.event.*;
import javax.swing.*;

/**
 * accept_write_access.java
 *
 * Registers a persistent AWTEventListener that fires the instant any window
 * opens. When the "API client needs write access action confirmation" dialog
 * appears, it immediately clicks the first visible button (Yes/Allow/OK).
 *
 * This runs inside the Gateway JVM permanently until the process exits.
 * No polling. No timing window. Fires on the AWT event dispatch thread
 * the exact moment the dialog becomes visible.
 */
public class accept_write_access {

    static PrintStream log;
    static final String DIALOG_TITLE = "API client needs write access action confirmation";

    public static void agentmain(String args, Instrumentation inst) {
        try {
            log = new PrintStream(new FileOutputStream("/tmp/accept_write_access.log", false));
        } catch (Exception e) {
            log = System.err;
        }
        log.println("[AWA] Agent started — registering AWTEventListener");
        log.flush();

        // Register on the EDT so it's permanent for the JVM lifetime
        EventQueue.invokeLater(() -> {
            try {
                Toolkit.getDefaultToolkit().addAWTEventListener(event -> {
                    if (event.getID() != WindowEvent.WINDOW_OPENED &&
                        event.getID() != ComponentEvent.COMPONENT_SHOWN) return;

                    Object src = event.getSource();
                    if (!(src instanceof Dialog)) return;

                    Dialog d = (Dialog) src;
                    String title = d.getTitle();
                    log.println("[AWA] Window event: '" + title + "' id=" + event.getID());
                    log.flush();

                    if (!DIALOG_TITLE.equals(title)) return;

                    log.println("[AWA] *** WRITE-ACCESS DIALOG DETECTED — clicking Yes ***");
                    log.flush();

                    // Click on EDT immediately
                    EventQueue.invokeLater(() -> {
                        try {
                            clickFirstButton(d);
                        } catch (Exception e) {
                            log.println("[AWA] click error: " + e);
                            log.flush();
                        }
                    });

                }, AWTEvent.WINDOW_EVENT_MASK | AWTEvent.COMPONENT_EVENT_MASK);

                log.println("[AWA] AWTEventListener registered successfully");
                log.flush();

            } catch (Exception e) {
                log.println("[AWA] registration error: " + e);
                e.printStackTrace(log);
                log.flush();
            }
        });
    }

    static void clickFirstButton(Dialog d) {
        // Walk component tree and click the first visible AbstractButton
        clickButton(d);
        log.println("[AWA] Done");
        log.flush();
    }

    static boolean clickButton(Container c) {
        for (Component comp : c.getComponents()) {
            if (comp instanceof AbstractButton && comp.isVisible() && comp.isEnabled()) {
                AbstractButton btn = (AbstractButton) comp;
                try {
                    String text = btn.getText();
                    log.println("[AWA] Clicking button: '" + text + "'");
                    log.flush();
                    btn.doClick();
                    return true;
                } catch (Exception e) {
                    log.println("[AWA] doClick error: " + e);
                }
            }
            if (comp instanceof Container) {
                if (clickButton((Container) comp)) return true;
            }
        }
        return false;
    }
}
