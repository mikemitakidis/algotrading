import java.lang.instrument.*;
import java.io.*;
import java.awt.*;
import java.awt.event.*;
import java.util.*;
import javax.swing.*;

public class fix_readonly_final {
    static PrintStream log;

    public static void agentmain(String args, Instrumentation inst) {
        EventQueue.invokeLater(() -> {
            try { log = new PrintStream(new FileOutputStream("/tmp/fix_final.log", false)); }
            catch (Exception e) { log = System.err; }
            try { run(); }
            catch (Exception e) { log.println("ERROR: " + e); e.printStackTrace(log); }
            log.flush();
        });
    }

    static List<Component> all(Container c) {
        List<Component> r = new ArrayList<>();
        for (Component ch : c.getComponents()) {
            r.add(ch);
            if (ch instanceof Container) r.addAll(all((Container)ch));
        }
        return r;
    }

    static void run() throws Exception {
        log.println("=== fix_readonly_final started ===");

        // Hide Login Messages permanently
        javax.swing.Timer hider = new javax.swing.Timer(300, e -> {
            for (Window w : Window.getWindows()) {
                String t = w instanceof Dialog ? ((Dialog)w).getTitle() :
                           w instanceof Frame  ? ((Frame)w).getTitle()  : "";
                if ("Login Messages".equals(t) && w.isShowing()) {
                    w.setVisible(false);
                    log.println("Hid Login Messages"); log.flush();
                }
            }
        });
        hider.setRepeats(true);
        hider.start();
        Thread.sleep(400);

        // Dismiss Exit Session via doClick
        for (Window w : Window.getWindows()) {
            String t = w instanceof Dialog ? ((Dialog)w).getTitle() : "";
            if (t != null && t.contains("Exit Session")) {
                log.println("Dismissing: " + t);
                for (Component c : all((Container)w)) {
                    if (c instanceof AbstractButton) {
                        try {
                            String bt = (String)c.getClass().getMethod("getText").invoke(c);
                            if ("OK".equals(bt) && c.isVisible()) {
                                log.println("doClick OK");
                                ((AbstractButton)c).doClick();
                            }
                        } catch (Exception ex) {}
                    }
                }
            }
        }
        Thread.sleep(600);

        // Fire Settings menu action directly (no Robot needed)
        for (Window w : Window.getWindows()) {
            if (!(w instanceof JFrame)) continue;
            JMenuBar bar = ((JFrame)w).getJMenuBar();
            if (bar == null) continue;
            for (int i = 0; i < bar.getMenuCount(); i++) {
                JMenu m = bar.getMenu(i);
                if (!"Configure".equals(m != null ? m.getText() : "")) continue;
                for (int j = 0; j < m.getItemCount(); j++) {
                    JMenuItem item = m.getItem(j);
                    if (item == null || !"Settings".equals(item.getText())) continue;
                    log.println("Firing Settings");
                    for (ActionListener al : item.getActionListeners())
                        al.actionPerformed(new ActionEvent(item,
                            ActionEvent.ACTION_PERFORMED, item.getActionCommand()));
                }
            }
        }

        // Poll for Settings dialog — uncheck Read-Only — click OK
        for (int tick = 0; tick < 40; tick++) {
            Thread.sleep(500);
            for (Window w : Window.getWindows()) {
                if (!(w instanceof JDialog) || !w.isShowing()) continue;
                String t = ((JDialog)w).getTitle();
                if ("Login Messages".equals(t) || (t != null && t.contains("Exit Session"))) continue;
                log.println("Settings dialog: '" + t + "'");
                JDialog d = (JDialog) w;
                boolean unchecked = false;
                for (Component c : all(d)) {
                    if (!(c instanceof JCheckBox)) continue;
                    JCheckBox cb = (JCheckBox) c;
                    String ct = cb.getText();
                    log.println("  CB: '" + ct + "' sel=" + cb.isSelected());
                    if (ct != null && ct.toLowerCase().contains("read")) {
                        log.println("  UNCHECKING");
                        if (cb.isSelected()) cb.doClick();
                        unchecked = true;
                    }
                }
                // Click OK
                for (Component c : all(d)) {
                    if (!(c instanceof AbstractButton)) continue;
                    try {
                        String bt = (String)c.getClass().getMethod("getText").invoke(c);
                        if ("OK".equals(bt) && c.isVisible()) {
                            log.println("Clicking OK"); ((AbstractButton)c).doClick();
                        }
                    } catch (Exception ex) {}
                }
                hider.stop();
                log.println("DONE. unchecked=" + unchecked);
                log.flush();
                return;
            }
            if (tick % 4 == 0) { log.println("tick=" + tick); log.flush(); }
        }
        log.println("Timed out");
        log.flush();
    }
}
