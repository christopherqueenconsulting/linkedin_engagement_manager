-- c2pa_signed records whether the post's AI image carries an embedded C2PA manifest (#2634).
-- NULL = never signed or never recorded (every row before this column); 1 = signed this run;
-- 0 = signing was attempted and failed. A reader treats anything but 1 as "not signed".
ALTER TABLE posts
    ADD COLUMN c2pa_signed TINYINT(1) NULL;
