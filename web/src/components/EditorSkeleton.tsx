import styles from "./EditorView.module.css";

export function EditorSkeleton() {
  return (
    <section className={`${styles.editor} ${styles.entering}`} aria-label="Loading video">
      <div className={styles.skeletonBack} />
      <div className={styles.skeletonLine} />
      <div className={`${styles.skeletonLine} ${styles.skeletonShort}`} />
    </section>
  );
}
